/**
 * Port of `app/deployment_helper.py`: which modules deploy, in what order, and how
 * `--optimize-deployment` groups them.
 *
 * The ordering is the whole point. A module's stack reads settings that an earlier module's stack
 * wrote, so `analytics` before `cluster-manager` is not a preference. Priorities come from the
 * module metadata table in `config/cluster-config.ts`, so there is one copy of them.
 */

import { ClusterConfig, GeneralException, MODULE_METADATA, type ModuleInfo } from '../config/cluster-config.ts';
import { buildBootstrapContext } from './bootstrap-context.ts';
import { CdkInvoker, type Deps } from './cdk-invoker.ts';

/** `deployment_helper.py:194`: the stagger between two modules of the same priority group. */
export const OPTIMIZED_DEPLOYMENT_STAGGER_MS = 10_000;

const MODULE_TYPE_CONFIG = 'config';

const PRIORITY_BY_MODULE_NAME = new Map(MODULE_METADATA.map((entry) => [entry.name, entry.deployment_priority]));

/**
 * `ModuleMetadataHelper.get_module_deployment_priority(module_name=...)`: the priority comes from
 * the module NAME, never from the table row, so an unjoined `ModuleInfo` still orders correctly.
 */
export function deploymentPriority(moduleName: string): number {
  const priority = PRIORITY_BY_MODULE_NAME.get(moduleName);
  if (priority === undefined) throw new GeneralException(`module not found for name: ${moduleName}`);
  return priority;
}

export interface DeploymentHelperOptions {
  clusterName: string;
  awsRegion: string;
  moduleSet: string;
  awsProfile?: string;
  terminationProtection?: boolean;
  deploymentId?: string;
  upgrade?: boolean;
  allModules?: boolean;
  forceBuildBootstrap?: boolean;
  optimizeDeployment?: boolean;
  rollback?: boolean;
  moduleIds?: readonly string[];
  allowReplacement?: readonly string[];
  allowReplacementOfType?: ReadonlyMap<string, string>;
  staggerMs?: number;
  deps: Deps;
}

/**
 * `get_deployment_order` and `get_optimized_deployment_order` as pure functions over the modules
 * table, so both the CLI and its tests share one implementation.
 */
export function deploymentOrder(
  modules: ModuleInfo[],
  moduleIds: readonly string[],
  upgrade: boolean,
): string[] {
  const byId = new Map(modules.map((module) => [module.module_id, module]));
  const selected: Array<{ moduleId: string; priority: number }> = [];
  for (const moduleId of moduleIds) {
    const module = byId.get(moduleId);
    if (module === undefined) continue;
    if (module.type === MODULE_TYPE_CONFIG) continue;
    if (module.status === 'deployed' && !upgrade) continue;
    selected.push({ moduleId, priority: deploymentPriority(module.name) });
  }
  // Python's list.sort is stable, so equal priorities keep the modules-table order.
  selected.sort((a, b) => a.priority - b.priority);
  return selected.map((entry) => entry.moduleId);
}

/** The same selection, grouped by priority, in first-seen priority order. */
export function optimizedDeploymentOrder(
  modules: ModuleInfo[],
  moduleIds: readonly string[],
  upgrade: boolean,
): string[][] {
  const byId = new Map(modules.map((module) => [module.module_id, module]));
  const groups = new Map<number, string[]>();
  for (const moduleId of deploymentOrder(modules, moduleIds, upgrade)) {
    const moduleName = byId.get(moduleId)?.name;
    if (moduleName === undefined) continue;
    const priority = deploymentPriority(moduleName);
    const group = groups.get(priority);
    if (group === undefined) groups.set(priority, [moduleId]);
    else group.push(moduleId);
  }
  return [...groups.values()];
}

export class DeploymentHelper {
  readonly clusterName: string;
  readonly awsRegion: string;
  readonly moduleSet: string;
  readonly deploymentId: string;
  readonly upgrade: boolean;
  readonly allModules: boolean;
  private readonly options: DeploymentHelperOptions;
  private readonly deps: Deps;
  private readonly staggerMs: number;
  private config: ClusterConfig;

  private constructor(options: DeploymentHelperOptions, config: ClusterConfig, deploymentId: string) {
    this.options = options;
    // A caller can replace the provider for isolated tests. Normal deploys use the ported context.
    // The default is layered over the caller's object, never copied onto a new one: a copy drops the
    // prototype methods of a class-based Deps, and it hides a hook the caller replaces after this
    // constructor runs.
    this.deps =
      options.deps.bootstrapContext === undefined
        ? new Proxy(options.deps, {
            get: (target, property, receiver) =>
              property === 'bootstrapContext'
                ? buildBootstrapContext
                : Reflect.get(target, property, receiver),
          })
        : options.deps;
    this.clusterName = options.clusterName;
    this.awsRegion = options.awsRegion;
    this.moduleSet = options.moduleSet;
    this.deploymentId = deploymentId;
    this.upgrade = options.upgrade === true;
    this.allModules = options.allModules === true;
    this.staggerMs = options.staggerMs ?? OPTIMIZED_DEPLOYMENT_STAGGER_MS;
    this.config = config;
  }

  static async open(options: DeploymentHelperOptions): Promise<DeploymentHelper> {
    const config = await ClusterConfig.fromDynamoDb(options.clusterName, options.awsRegion, {
      moduleSet: options.moduleSet,
      scan: options.deps.scan,
    });
    const deploymentId =
      options.deploymentId !== undefined && options.deploymentId !== ''
        ? options.deploymentId
        : options.deps.uuid();
    return new DeploymentHelper(options, config, deploymentId);
  }

  private moduleIds(): string[] {
    if (this.allModules) return this.config.modules().map((module) => module.module_id);
    return [...(this.options.moduleIds ?? [])];
  }

  getDeploymentOrder(): string[] {
    return deploymentOrder(this.config.modules(), this.moduleIds(), this.upgrade);
  }

  /** Module names resolved from the same selected deployment order used for invocation. */
  getDeploymentModuleNames(): string[] {
    return this.getDeploymentOrder().flatMap((moduleId) => {
      const name = this.config.moduleInfoById(moduleId)?.name;
      return name === undefined ? [] : [name];
    });
  }

  getOptimizedDeploymentOrder(): string[][] {
    return optimizedDeploymentOrder(this.config.modules(), this.moduleIds(), this.upgrade);
  }

  private printNoOpMessage(): void {
    if (this.upgrade) {
      this.deps.out('could not find any modules to upgrade.');
      return;
    }
    const moduleIds = this.moduleIds();
    if (moduleIds.length === 1) {
      this.deps.out(
        `${moduleIds[0]} is already deployed. use the --upgrade flag to upgrade or re-deploy the module.`,
      );
    } else {
      this.deps.out(
        `[${moduleIds.join(', ')}] are already deployed. use the --upgrade flag to re-deploy these modules.`,
      );
    }
  }

  async deployModule(moduleId: string): Promise<void> {
    const moduleInfo = this.config.moduleInfoById(moduleId);
    if (moduleInfo === undefined) throw new GeneralException(`module not found for module_id: ${moduleId}`);
    this.deps.out(`deploying module: ${moduleInfo.name}, module id: ${moduleId}`);
    const invoker = await CdkInvoker.open({
      clusterName: this.clusterName,
      awsRegion: this.awsRegion,
      moduleId,
      moduleSet: this.moduleSet,
      awsProfile: this.options.awsProfile,
      deploymentId: this.deploymentId,
      terminationProtection: this.options.terminationProtection,
      rollback: this.options.rollback,
      allowReplacement: this.options.allowReplacement,
      allowReplacementOfType: this.options.allowReplacementOfType,
      deps: this.deps,
    });
    await invoker.invoke({ forceBuildBootstrap: this.options.forceBuildBootstrap });
  }

  /**
   * Re-read the modules table. A deployment can take more than an hour, so hourly STS credentials
   * can expire mid-run: Python rebuilt the `ClusterConfigDB` and the boto session on
   * `ExpiredTokenException`. The SDK's credential provider refreshes on the next call, so retrying
   * the scan once is the whole fix; anything else is a real error.
   */
  private async refreshModules(): Promise<void> {
    const read = (): Promise<ClusterConfig> =>
      ClusterConfig.fromDynamoDb(this.clusterName, this.awsRegion, {
        moduleSet: this.moduleSet,
        scan: this.deps.scan,
      });
    try {
      this.config = await read();
    } catch (error) {
      if ((error as { name?: string }).name !== 'ExpiredTokenException') throw error;
      this.config = await read();
    }
  }

  async invoke(): Promise<void> {
    if (this.options.optimizeDeployment === true && this.moduleIds().length > 1) {
      const groups = this.getOptimizedDeploymentOrder();
      if (groups.length === 0) {
        this.printNoOpMessage();
        return;
      }
      this.deps.out(`optimized deployment order: ${JSON.stringify(groups)}`);

      for (const group of groups) {
        const failures = new Map<string, unknown>();
        const running: Array<Promise<void>> = [];
        for (const moduleId of group) {
          running.push(
            this.deployModule(moduleId).catch((error: unknown) => {
              failures.set(moduleId, error);
            }),
          );
          // process the next entry after 10 seconds.
          await this.deps.sleep(this.staggerMs);
        }
        await Promise.all(running);

        // The status check below cannot see this: a module already deployed by the previous
        // release still reads 'deployed' after a failed re-deploy.
        if (failures.size > 0) {
          const detail = [...failures.entries()]
            .map(([moduleId, error]) => `${moduleId} (${errorMessage(error)})`)
            .join(', ');
          throw new GeneralException(`deployment failed. could not deploy module(s): ${detail}`);
        }

        await this.refreshModules();
        for (const moduleId of group) {
          const moduleInfo = this.config.moduleInfoById(moduleId);
          if (moduleInfo?.status !== 'deployed') {
            throw new GeneralException(
              `Module ${moduleId} on ${this.clusterName} is not deployed after its stack run. See CloudFormation events for stack ${this.clusterName}-${moduleId}, then re-run ideactl deploy ${moduleId} --cluster-name ${this.clusterName} --aws-region ${this.awsRegion}.`,
            );
          }
        }
      }
      return;
    }

    const order = this.getDeploymentOrder();
    if (order.length === 0) {
      this.printNoOpMessage();
      return;
    }
    for (const moduleId of order) {
      await this.deployModule(moduleId);
    }
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
