/**
 * Shared-storage attach and create command workflow.
 *
 * The interactive questionnaire is represented by an injected prompt. Its
 * result is converted to the same settings map the administrator stores.
 */

import { Command } from "commander";

import { ClusterConfig, ClusterConfigError, isEmpty } from "../../config/cluster-config.ts";

export const NEXT_STEP_EXIT = "Exit";
export const NEXT_STEP_UPDATE_SETTINGS = "Update Cluster Settings and Exit";
export const NEXT_STEP_UPGRADE_MODULE = "Deploy Module: Shared Storage";
export const NEXT_STEP_DEPLOY_MODULE = "Upgrade Module: Shared Storage";

export interface SharedStorageApi {
  describeFileSystems(input: { FileSystemId?: string; FileSystemIds?: string[] }): Promise<{ FileSystems?: Array<Record<string, unknown>> }>;
  describeFileCaches(input: { FileCacheIds: string[] }): Promise<{ FileCaches?: Array<Record<string, unknown>> }>;
  describeStorageVirtualMachines(input: { StorageVirtualMachineIds: string[] }): Promise<{ StorageVirtualMachines?: Array<Record<string, unknown>> }>;
  describeVolumes(input: { VolumeIds: string[] }): Promise<{ Volumes?: Array<Record<string, unknown>> }>;
}

export interface SharedStorageDeps {
  config?: ClusterConfig;
  storage: SharedStorageApi;
  awsDnsSuffix(): Promise<string>;
  prompt(useExistingFs: boolean, existingCluster: boolean): Promise<Record<string, unknown>>;
  promptNextStep(choices: string[]): Promise<string>;
  syncSettings(entries: Array<{ key: string; value: unknown }>): Promise<void>;
  deploy(moduleId: string, upgrade: boolean): Promise<void>;
  out(line: string): void;
}

/** Builds dependencies for the profile selected by one shared-storage command. */
export type SharedStorageDepsFactory = (options: SharedStorageOptions & {
  awsProfile?: string;
}) => Promise<SharedStorageDeps>;

type SharedStorageDepsSource = SharedStorageDeps | SharedStorageDepsFactory;

export interface SharedStorageOptions {
  clusterName?: string;
  awsRegion: string;
  kmsKeyId?: string;
}

function text(params: Record<string, unknown>, key: string, fallback = ""): string {
  const value = params[key];
  return typeof value === "string" && value.trim() !== "" ? value : fallback;
}

function bool(params: Record<string, unknown>, key: string, fallback = false): boolean {
  const value = params[key];
  if (typeof value === "boolean") return value;
  if (typeof value === "string") return ["true", "yes", "y", "1", "on"].includes(value.toLowerCase());
  return fallback;
}

function list(params: Record<string, unknown>, key: string): unknown[] {
  return Array.isArray(params[key]) ? params[key] as unknown[] : [];
}

function object(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function delimited(value: string, fallback: string[]): string[] {
  const entries = value.split(",").map((entry) => entry.trim().toLowerCase()).filter((entry) => entry !== "");
  return entries.length === 0 ? fallback : entries;
}

function common(params: Record<string, unknown>): Record<string, unknown> {
  const provider = text(params, "shared_storage_provider");
  const result: Record<string, unknown> = {
    title: text(params, "shared_storage_title"),
    provider,
    scope: delimited(text(params, "shared_storage_scope"), ["cluster"]),
  };
  const scope = result.scope as string[];
  if (scope.includes("module")) result.modules = list(params, "shared_storage_scope_modules");
  if (scope.includes("project")) result.projects = delimited(text(params, "shared_storage_scope_projects"), ["default"]);
  if (scope.includes("scheduler:queue-profile")) result.queue_profiles = delimited(text(params, "shared_storage_scope_queue_profiles"), ["compute"]);
  if (["fsx_netapp_ontap", "fsx_windows_file_server"].includes(provider)) result.mount_drive = text(params, "shared_storage_mount_drive", "Z:");
  if (provider !== "fsx_windows_file_server") {
    result.mount_dir = text(params, "shared_storage_mount_dir");
    result.mount_options = text(params, `${provider}.mount_options`);
  }
  return result;
}

function requireValue(value: string, name: string): string {
  if (isEmpty(value)) throw new ClusterConfigError(`${name} is required`);
  return value;
}

/** Build the precise provider map from questionnaire values and describe responses. */
export async function buildSharedStorageConfig(
  deps: Pick<SharedStorageDeps, "storage" | "awsDnsSuffix">,
  options: SharedStorageOptions,
  params: Record<string, unknown>,
  useExistingFs: boolean,
): Promise<Record<string, unknown>> {
  const name = requireValue(text(params, "shared_storage_name"), "shared_storage_name");
  const provider = text(params, "shared_storage_provider");
  const base = common(params);
  if (provider === "efs") {
    if (!useExistingFs) {
      const transition = text(params, "efs.transition_to_ia");
      return { [name]: { ...base, efs: {
        kms_key_id: options.kmsKeyId,
        encrypted: true,
        throughput_mode: text(params, "efs.throughput_mode"),
        performance_mode: text(params, "efs.performance_mode"),
        removal_policy: text(params, "efs.removal_policy", "DESTROY"),
        cloudwatch_monitoring: bool(params, "efs.cloudwatch_monitoring"),
        transition_to_ia: transition === "DISABLED" ? null : transition,
      } } };
    }
    const fileSystemId = requireValue(text(params, "efs.file_system_id"), "efs.file_system_id");
    const fileSystem = (await deps.storage.describeFileSystems({ FileSystemId: fileSystemId })).FileSystems?.[0] ?? {};
    return { [name]: { ...base, efs: {
      use_existing_fs: true,
      file_system_id: fileSystemId,
      dns: `${fileSystemId}.efs.${options.awsRegion}.${await deps.awsDnsSuffix()}`,
      encrypted: bool(fileSystem, "Encrypted"),
    } } };
  }
  if (provider === "fsx_cache") {
    const id = requireValue(text(params, "fsx_cache.file_system_id"), "fsx_cache.file_system_id");
    const fs = (await deps.storage.describeFileCaches({ FileCacheIds: [id] })).FileCaches?.[0] ?? {};
    const lustre = object(fs.LustreConfiguration);
    return { [name]: { ...base, fsx_cache: { use_existing_fs: true, file_system_id: id, dns: text(fs, "DNSName"), mount_name: text(lustre, "MountName"), version: text(fs, "FileCacheTypeVersion") } } };
  }
  if (provider === "fsx_lustre") {
    const id = requireValue(text(params, "fsx_lustre.file_system_id"), "fsx_lustre.file_system_id");
    const fs = (await deps.storage.describeFileSystems({ FileSystemIds: [id] })).FileSystems?.[0] ?? {};
    const lustre = object(fs.LustreConfiguration);
    return { [name]: { ...base, fsx_lustre: { use_existing_fs: true, file_system_id: id, dns: text(fs, "DNSName"), mount_name: text(lustre, "MountName"), version: text(fs, "FileSystemTypeVersion") } } };
  }
  if (provider === "fsx_netapp_ontap") {
    const fileSystemId = requireValue(text(params, "fsx_netapp_ontap.file_system_id"), "fsx_netapp_ontap.file_system_id");
    const svmId = requireValue(text(params, "fsx_netapp_ontap.svm_id"), "fsx_netapp_ontap.svm_id");
    const volumeId = requireValue(text(params, "fsx_netapp_ontap.volume_id"), "fsx_netapp_ontap.volume_id");
    const svm = (await deps.storage.describeStorageVirtualMachines({ StorageVirtualMachineIds: [svmId] })).StorageVirtualMachines?.[0] ?? {};
    const endpoints = object(svm.Endpoints);
    const volume = (await deps.storage.describeVolumes({ VolumeIds: [volumeId] })).Volumes?.[0] ?? {};
    const ontap = object(volume.OntapConfiguration);
    return { [name]: { ...base, fsx_netapp_ontap: { use_existing_fs: true, file_system_id: fileSystemId, svm: {
      svm_id: svmId, smb_dns: text(object(endpoints.Smb), "DNSName"), nfs_dns: text(object(endpoints.Nfs), "DNSName"),
      management_dns: text(object(endpoints.Management), "DNSName"), iscsi_dns: text(object(endpoints.Iscsi), "DNSName"),
    }, volume: { volume_id: volumeId, volume_path: text(ontap, "JunctionPath"), security_style: text(ontap, "SecurityStyle"), cifs_share_name: text(params, "fsx_netapp_ontap.cifs_share_name") } } } };
  }
  if (provider === "fsx_openzfs") {
    const fsId = requireValue(text(params, "fsx_openzfs.file_system_id"), "fsx_openzfs.file_system_id");
    const volumeId = requireValue(text(params, "fsx_openzfs.volume_id"), "fsx_openzfs.volume_id");
    const fs = (await deps.storage.describeFileSystems({ FileSystemIds: [fsId] })).FileSystems?.[0] ?? {};
    const volume = (await deps.storage.describeVolumes({ VolumeIds: [volumeId] })).Volumes?.[0] ?? {};
    return { [name]: { ...base, fsx_openzfs: { use_existing_fs: true, file_system_id: fsId, dns: text(fs, "DNSName"), volume_id: volumeId, volume_path: text(object(volume.OpenZFSConfiguration), "VolumePath") } } };
  }
  if (provider === "fsx_windows_file_server") {
    const id = requireValue(text(params, "fsx_windows_file_server.file_system_id"), "fsx_windows_file_server.file_system_id");
    const fs = (await deps.storage.describeFileSystems({ FileSystemIds: [id] })).FileSystems?.[0] ?? {};
    return { [name]: { ...base, fsx_windows_file_server: { use_existing_fs: true, file_system_id: id, dns: text(fs, "DNSName"), preferred_file_server_ip: text(object(fs.WindowsConfiguration), "PreferredFileServerIp") } } };
  }
  throw new ClusterConfigError(`shared storage provider: ${provider} not supported`);
}

function flatten(value: Record<string, unknown>, prefix = ""): Array<{ key: string; value: unknown }> {
  const entries: Array<{ key: string; value: unknown }> = [];
  for (const [key, item] of Object.entries(value)) {
    const path = prefix === "" ? key : `${prefix}.${key}`;
    if (typeof item === "object" && item !== null && !Array.isArray(item)) entries.push(...flatten(item as Record<string, unknown>, path));
    else entries.push({ key: path, value: item });
  }
  return entries;
}

/** Run the prompt, write selected settings, and optionally deploy the shared-storage module. */
export async function manageSharedStorage(deps: SharedStorageDeps, options: SharedStorageOptions, useExistingFs: boolean): Promise<void> {
  const config = deps.config;
  const clusterModule = config?.moduleInfoById(config.moduleId("cluster"));
  const sharedModuleId = config?.moduleId("shared-storage");
  const sharedModule = sharedModuleId === undefined ? undefined : config?.moduleInfoById(sharedModuleId);
  const existingCluster = clusterModule?.status === "deployed";
  const params = await deps.prompt(useExistingFs, existingCluster === true);
  const storage = await buildSharedStorageConfig(deps, options, params, useExistingFs);
  deps.out(JSON.stringify(storage, null, 2).replaceAll(": null", ": ~"));
  const choices = [NEXT_STEP_EXIT];
  if (config !== undefined) choices.push(NEXT_STEP_UPDATE_SETTINGS);
  if (!useExistingFs && existingCluster === true) choices.push(sharedModule?.status === "deployed" ? NEXT_STEP_UPGRADE_MODULE : NEXT_STEP_DEPLOY_MODULE);
  const next = choices.length === 1 ? NEXT_STEP_EXIT : await deps.promptNextStep(choices);
  if (next === NEXT_STEP_EXIT) return;
  if (sharedModuleId === undefined) throw new ClusterConfigError("shared-storage module id not found");
  await deps.syncSettings(flatten(storage, sharedModuleId));
  if (next === NEXT_STEP_DEPLOY_MODULE || next === NEXT_STEP_UPGRADE_MODULE) await deps.deploy(sharedModuleId, next === NEXT_STEP_UPGRADE_MODULE);
}

/** Register the `shared-storage` command group. */
export function registerSharedStorageCommands(program: Command, deps: SharedStorageDepsSource): Command {
  const resolveDeps = async (options: SharedStorageOptions & {
    awsProfile?: string;
  }): Promise<SharedStorageDeps> => typeof deps === "function" ? deps(options) : deps;
  const group = program.command("shared-storage").description("shared storage commands");
  for (const [name, existing] of [["add-file-system", false], ["attach-file-system", true]] as const) {
    group.command(name).option("--cluster-name <cluster-name>").requiredOption("--aws-region <aws-region>").option("--aws-profile <aws-profile>").option("--kms-key-id <kms-key-id>")
      .action(async (options: SharedStorageOptions & { awsProfile?: string }) => {
        await manageSharedStorage(await resolveDeps(options), options, existing);
      });
  }
  return group;
}
