/**
 * One security group, then a pass over every top-level key under `shared-storage`. A key is a file
 * system only when its value is a config subtree carrying both `provider` and `mount_dir`, which is
 * what keeps `deployment_id` and `security_group_id` out of the loop. `use_existing_fs` registers an
 * id and provisions nothing; `efs` and `fsx_lustre` provision; the other three providers
 * (`fsx_cache`, `fsx_netapp_ontap`, `fsx_openzfs`, `fsx_windows_file_server`) are accepted by the
 * validation and then provision nothing, so a cluster whose `data` volume is ONTAP synthesizes with
 * no `data-storage-efs` at all.
 *
 * Two quirks the live templates depend on:
 *
 *  - the EFS file systems carry `DeletionPolicy` and no `UpdateReplacePolicy`. Nothing here may
 *    call `applyRemovalPolicy`.
 *  - the `dns` guard reads `shared-storage.<module id>.dns`, not the file system's key, so it never
 *    finds a value and the `dns` setting is always rewritten. Every node's bootstrap mounts /apps
 *    and /data from that key.
 */

import type { StackBuildProps } from '../app.ts';
import { IdeaBaseStack } from '../base-stack.ts';
import { ConfigKeyNotFound, isEmpty, type ClusterConfig } from '../../config/cluster-config.ts';
import { ExistingSocaCluster } from '../constructs/existing-resources.ts';
import { SharedStorageSecurityGroup } from '../constructs/network.ts';
import { AmazonEFS, FSxForLustre, valueAsDict } from '../constructs/storage.ts';

export const STORAGE_PROVIDER_EFS = 'efs';
export const STORAGE_PROVIDER_FSX_CACHE = 'fsx_cache';
export const STORAGE_PROVIDER_FSX_LUSTRE = 'fsx_lustre';
export const STORAGE_PROVIDER_FSX_NETAPP_ONTAP = 'fsx_netapp_ontap';
export const STORAGE_PROVIDER_FSX_OPENZFS = 'fsx_openzfs';
export const STORAGE_PROVIDER_FSX_WINDOWS_FILE_SERVER = 'fsx_windows_file_server';

/** `constants.SUPPORTED_STORAGE_PROVIDERS`, in declaration order. */
export const SUPPORTED_STORAGE_PROVIDERS: readonly string[] = [
  STORAGE_PROVIDER_EFS,
  STORAGE_PROVIDER_FSX_CACHE,
  STORAGE_PROVIDER_FSX_LUSTRE,
  STORAGE_PROVIDER_FSX_NETAPP_ONTAP,
  STORAGE_PROVIDER_FSX_OPENZFS,
  STORAGE_PROVIDER_FSX_WINDOWS_FILE_SERVER,
];

/** Storage file-system properties. */
export interface FileSystemHolder {
  name: string;
  provider: string;
  fileSystemId: string;
  /** Absent for a file system that was registered from `use_existing_fs` rather than provisioned. */
  fileSystem?: AmazonEFS | FSxForLustre;
}

function isSubtree(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * `config.getConfig('shared-storage', required=true)`: the whole module subtree.
 *
 * `getRealKey` renders a single-segment key as `<module id>.`, which yields an empty segment.
 * This helper skips empty segments and throws `ConfigKeyNotFound` for a missing required value.
 */
function moduleSubtree(config: ClusterConfig, moduleKey: string): Record<string, unknown> {
  const realKey = config.getRealKey(moduleKey);
  const tree = (config as unknown as { tree: Record<string, unknown> }).tree;
  let node: unknown = tree;
  for (const part of realKey.split('.').filter((segment) => segment !== '')) {
    if (!isSubtree(node) || !(part in node)) {
      throw new ConfigKeyNotFound(`'${part}', key: ${realKey}`);
    }
    node = node[part];
  }
  return isSubtree(node) && !isEmpty(node) ? node : {};
}

/** `ConfigTree.get` with a dotted path, scoped to one storage config subtree. */
function subtreeGet(subtree: Record<string, unknown>, path: string): unknown {
  let node: unknown = subtree;
  for (const part of path.split('.')) {
    if (!isSubtree(node)) return undefined;
    node = node[part];
  }
  return node;
}

export class SharedStorageStack extends IdeaBaseStack {
  readonly cluster: ExistingSocaCluster;
  readonly fileSystems: FileSystemHolder[] = [];
  securityGroup: SharedStorageSecurityGroup | undefined;

  constructor(props: StackBuildProps) {
    super({
      scope: props.app,
      ctx: props.ctx,
      moduleName: props.moduleName,
      deploymentId: props.deploymentId,
      terminationProtection: props.terminationProtection,
      env: props.env,
    });

    this.cluster = new ExistingSocaCluster(this.context, this.stack);

    // `apps` and `data` are mandatory: assert the two keys exist before anything is built.
    this.context.config.getString('shared-storage.apps.provider', undefined, { required: true });
    this.context.config.getString('shared-storage.data.provider', undefined, { required: true });

    this.buildSecurityGroup();
    this.buildSharedStorage();
    this.buildClusterSettings();
  }

  buildSecurityGroup(): void {
    this.securityGroup = new SharedStorageSecurityGroup(
      this.context,
      'shared-storage-security-group',
      this.stack,
      this.cluster.vpc,
    );
  }

  buildSharedStorage(): void {
    const storageConfigs = moduleSubtree(this.context.config, 'shared-storage');

    for (const [name, value] of Object.entries(storageConfigs)) {
      // Non-storage keys under `shared-storage` (deployment_id, security_group_id) are scalars.
      if (!isSubtree(value)) continue;

      const provider = subtreeGet(value, 'provider');
      const mountDir = subtreeGet(value, 'mount_dir');
      if (isEmpty(provider) || isEmpty(mountDir)) continue;

      const providerName = provider as string;
      if (!SUPPORTED_STORAGE_PROVIDERS.includes(providerName)) {
        throw new Error(`file system provider: ${providerName} not supported`);
      }

      if (subtreeGet(value, `${providerName}.use_existing_fs`)) {
        const fileSystemId = subtreeGet(value, `${providerName}.file_system_id`);
        if (isEmpty(fileSystemId)) {
          throw new Error(`shared-storage.${name}.${providerName}.file_system_id is required`);
        }
        this.fileSystems.push({ name, provider: providerName, fileSystemId: fileSystemId as string });
        continue;
      }

      if (providerName === STORAGE_PROVIDER_EFS) {
        this.buildEfs(name, value);
      } else if (providerName === STORAGE_PROVIDER_FSX_LUSTRE) {
        this.buildFsxLustre(name, value);
      }
    }
  }

  buildEfs(name: string, storageConfig: Record<string, unknown>): void {
    const efs = new AmazonEFS(this.context, `${name}-storage-efs`, this.stack, {
      vpc: this.cluster.vpc,
      efsConfig: valueAsDict('efs', storageConfig),
      securityGroup: this.securityGroup as SharedStorageSecurityGroup,
      subnets: this.cluster.privateSubnets,
    });
    this.fileSystems.push({
      name,
      provider: STORAGE_PROVIDER_EFS,
      fileSystemId: efs.fileSystem.ref,
      fileSystem: efs,
    });
  }

  buildFsxLustre(name: string, storageConfig: Record<string, unknown>): void {
    const fsxLustre = new FSxForLustre(this.context, `${name}-storage-fsx-lustre`, this.stack, {
      vpc: this.cluster.vpc,
      fsxLustreConfig: valueAsDict('fsx_lustre', storageConfig),
      securityGroup: this.securityGroup as SharedStorageSecurityGroup,
      subnets: this.cluster.privateSubnets,
    });
    this.fileSystems.push({
      name,
      provider: STORAGE_PROVIDER_FSX_LUSTRE,
      fileSystemId: fsxLustre.fileSystem.ref,
      fileSystem: fsxLustre,
    });
  }

  /** `FileSystemHolder.file_system_dns`: `<id>.<efs|fsx>.<region>.<dns suffix>`. */
  fileSystemDns(fileSystem: FileSystemHolder): string {
    const config = this.context.config;
    const awsRegion = config.getString('cluster.aws.region', undefined, { required: true }) as string;
    const dnsSuffix = config.getString('cluster.aws.dns_suffix', undefined, { required: true }) as string;
    const fsType = fileSystem.provider === STORAGE_PROVIDER_EFS ? 'efs' : 'fsx';
    return `${fileSystem.fileSystemId}.${fsType}.${awsRegion}.${dnsSuffix}`;
  }

  buildClusterSettings(): void {
    const clusterSettings: Record<string, unknown> = {
      deployment_id: this.deploymentId,
      security_group_id: (this.securityGroup as SharedStorageSecurityGroup).securityGroupId,
    };

    for (const fileSystem of this.fileSystems) {
      if (
        fileSystem.provider === STORAGE_PROVIDER_EFS ||
        fileSystem.provider === STORAGE_PROVIDER_FSX_LUSTRE
      ) {
        // The module id, not the file system key: the lookup never resolves, so `dns` is always
        // rewritten. See the file header.
        const configuredDns = this.context.config.getString(`shared-storage.${this.moduleId}.dns`);
        if (isEmpty(configuredDns)) {
          clusterSettings[`${fileSystem.name}.${fileSystem.provider}.dns`] =
            this.fileSystemDns(fileSystem);
        }
      }

      // An existing file system already carries its settings; only provisioned ones write back.
      if (fileSystem.fileSystem === undefined) continue;
      clusterSettings[`${fileSystem.name}.${fileSystem.provider}.file_system_id`] =
        fileSystem.fileSystemId;
    }

    this.updateClusterSettings(clusterSettings);
  }
}

export function buildStack(props: StackBuildProps): void {
  new SharedStorageStack(props);
}
