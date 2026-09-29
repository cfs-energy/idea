import { ClusterConfig, GeneralException } from "../config/cluster-config.ts";
import type { Deps } from "./cdk-invoker.ts";

export interface HostPoolOptions {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
  moduleSet: string;
  refreshHosts?: boolean;
  hostPoolRerunCommand?: string;
}

export function refreshHostsCommand(command: string, options: object, modules: readonly string[] = []): string {
  const quote = (value: string) => `'${value.replaceAll("'", "'\\''")}'`;
  const argv = ["ideactl", command, ...modules.map(quote)];
  for (const [key, value] of Object.entries(options)) {
    if (value === undefined || key === "refreshHosts" || key === "modules") continue;
    const flag = key.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`);
    if (typeof value === "boolean" && key !== "terminationProtection") {
      if (value) argv.push(`--${flag}`);
      else if (key === "rollback") argv.push("--no-rollback");
    } else for (const item of Array.isArray(value) ? value : [value]) argv.push(`--${flag}`, quote(String(item)));
  }
  if (command === "deploy" && !argv.includes("--upgrade")) argv.push("--upgrade");
  return [...argv, "--refresh-hosts"].join(" ");
}

async function hostPoolState(deps: Deps, options: HostPoolOptions) {
  const config = await ClusterConfig.fromDynamoDb(options.clusterName, options.awsRegion, { moduleSet: options.moduleSet, scan: deps.scan });
  // Host-era clusters keep an EC2 bastion; only a container cluster has a host pool to gate.
  if (!config.getBool("ecs.enabled", false) || !config.getString("directoryservice.provider")) return undefined;
  const api = deps.containerHosts;
  if (!api) throw new GeneralException("Container host adapter is required for the directory join gate");
  const cluster = config.getString("ecs.cluster_name", undefined, { required: true }) as string;
  const capacityProvider = config.getString("ecs.capacity_provider", undefined, { required: true }) as string;
  const context = { awsRegion: options.awsRegion, awsProfile: options.awsProfile };
  const group = await api.hostGroup({ ...context, capacityProvider });
  const versions = await api.launchTemplateVersions({ ...context, name: group.name });
  const hosts = await api.containerInstances({ ...context, cluster });
  const stale = versions.instances.filter((host) => host.id !== versions.current.id || host.version !== versions.current.version);
  const cannotJoin = stale.filter((host) => !hosts.some((instance) => instance.instanceId === host.instanceId && instance.attributes?.["idea.directory"] === "joined"));
  if (!options.refreshHosts && cannotJoin.length) {
    throw new GeneralException(`Hosts ${cannotJoin.map((host) => host.instanceId).join(", ")} cannot join on their old launch template. Re-run: ${options.hostPoolRerunCommand ?? refreshHostsCommand("deploy", { clusterName: options.clusterName, awsRegion: options.awsRegion, awsProfile: options.awsProfile, moduleSet: options.moduleSet }, ["ecs"])}. The operator may instead refresh the host group themselves and re-run.`);
  }
  return { api, context, cluster, capacityProvider, group, stale };
}

/** Runs right after the ECS stack: fails fast on hosts that cannot join, replaces them only with --refresh-hosts. */
export async function refreshHostPool(deps: Deps, options: HostPoolOptions): Promise<void> {
  const state = await hostPoolState(deps, options);
  if (!state || !options.refreshHosts || !state.stale.length) return;
  const { api, context, group } = state;
  // Managed termination protection marks every host that runs tasks; the default (Wait) stalls an hour and fails.
  // Refresh replaces them and managed draining moves their tasks first. The template resolves its AMI through an
  // SSM parameter, which rules out SkipMatching, so the refresh replaces every host once a stale one exists.
  const preferences = { MinHealthyPercentage: 100, MaxHealthyPercentage: 200, InstanceWarmup: 300, ScaleInProtectedInstances: "Refresh" as const };
  let id: string;
  try {
    id = await api.startInstanceRefresh({ ...context, name: group.name, preferences });
  } catch (error) {
    if (!(error instanceof Error) || !/MaxHealthyPercentage/i.test(error.message)
        || !["ValidationError", "ValidationException", "InvalidParameterCombination", "InvalidParameterValue"].includes(error.name)) throw error;
    deps.out("Instance refresh API rejected MaxHealthyPercentage; retrying without it with MinHealthyPercentage: 100.");
    const { MaxHealthyPercentage: _maximum, ...fallback } = preferences;
    id = await api.startInstanceRefresh({ ...context, name: group.name, preferences: fallback });
  }
  const deadline = deps.now() + 90 * 60_000;
  while (true) {
    const refresh = await api.describeInstanceRefresh({ ...context, name: group.name, id });
    deps.out(`Host group ${group.name} refresh ${id}: ${refresh.status} (${refresh.percentageComplete ?? 0}%)${refresh.reason ? `: ${refresh.reason}` : ""}`);
    if (refresh.status === "Successful") return;
    if (["Failed", "Cancelled", "RollbackFailed", "RollbackSuccessful"].includes(refresh.status)) {
      throw new GeneralException(`Host group ${group.name} refresh ${id} ended ${refresh.status}: ${refresh.reason ?? "see instance refresh status"}`);
    }
    if (deps.now() >= deadline) throw new GeneralException(`Host group ${group.name} refresh ${id} exceeded 90 minutes; inspect the instance refresh before re-running.`);
    await deps.sleep(60_000);
  }
}

/**
 * Runs right before the bastion, the only service placed on joined hosts. Active Directory joins are
 * answered by the cluster manager, which a fresh install deploys after the ECS stack, so waiting
 * earlier would time out on a first deployment.
 */
export async function waitForDirectoryJoin(deps: Deps, options: HostPoolOptions): Promise<void> {
  const state = await hostPoolState(deps, options);
  if (!state) return;
  const { api, context, cluster, capacityProvider } = state;
  const deadline = deps.now() + 45 * 60_000;
  let reportAt = 0;
  while (true) {
    const currentGroup = await api.hostGroup({ ...context, capacityProvider });
    const currentHosts = await api.containerInstances({ ...context, cluster });
    const currentVersions = await api.launchTemplateVersions({ ...context, name: currentGroup.name });
    const pending = new Set(currentHosts.filter((host) => host.attributes?.["idea.directory"] !== "joined").map((host) => host.instanceId || host.arn));
    for (const id of currentGroup.instanceIds) if (!currentHosts.some((host) => host.instanceId === id)) pending.add(id);
    for (const host of currentVersions.instances) {
      if (!currentHosts.some((instance) => instance.instanceId === host.instanceId)
          || (options.refreshHosts && (host.id !== currentVersions.current.id || host.version !== currentVersions.current.version))) pending.add(host.instanceId);
    }
    if (!currentHosts.length || currentGroup.instanceIds.length < currentGroup.desiredCapacity) pending.add(`unregistered hosts in ${currentGroup.name}`);
    if (!pending.size) {
      deps.out("All container hosts carry idea.directory=joined.");
      return;
    }
    const names = [...pending].join(", ");
    if (deps.now() >= deadline) throw new GeneralException(`Directory join timed out after 45 minutes: ${names}. Inspect journal unit idea-directory-join.service on these hosts and restart it once the directory answers.`);
    if (deps.now() >= reportAt) {
      deps.out(`Waiting for idea.directory=joined: ${names}`);
      reportAt = deps.now() + 120_000;
    }
    await deps.sleep(30_000);
  }
}
