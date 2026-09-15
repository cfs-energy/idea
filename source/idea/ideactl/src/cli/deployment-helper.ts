/**
 * Port of `app/deployment_helper.py`: which modules deploy, in what order, and how
 * `--optimize-deployment` groups them.
 *
 * The ordering is the whole point. A module's stack reads settings that an earlier module's stack
 * wrote, so `analytics` before `cluster-manager` is not a preference. Priorities come from the
 * module metadata table in `config/cluster-config.ts`, so there is one copy of them.
 */

import { ClusterConfig, GeneralException, MODULE_METADATA, isEmpty, type ModuleInfo } from '../config/cluster-config.ts';
import { buildBootstrapContext } from './bootstrap-context.ts';
import { CdkInvoker, type ConfigWriter, type Deps } from './cdk-invoker.ts';
import { ensureSelfSignedCertificate, type CertificateRequest } from './certificates.ts';
import { mergeClientIpEntries } from './commands/utils.ts';

/** `deployment_helper.py:194`: the stagger between two modules of the same priority group. */
export const OPTIMIZED_DEPLOYMENT_STAGGER_MS = 10_000;

const MODULE_TYPE_CONFIG = 'config';
const MODULE_NAME_CLUSTER = 'cluster';
const MODULE_NAME_DIRECTORYSERVICE = 'directoryservice';
const MODULE_NAME_VIRTUAL_DESKTOP_CONTROLLER = 'virtual-desktop-controller';
const DIRECTORYSERVICE_OPENLDAP = 'openldap';

/**
 * One certificate the tool makes sure exists before a stack that reads it synthesizes, with the
 * configuration keys its ARNs are published under. The keys are the ones the stacks already read,
 * so a cluster deployed before this change already carries the rows and nothing regenerates.
 */
export interface CertificateHook {
  request: CertificateRequest;
  certificateKey: string;
  privateKeyKey: string;
  /** Only the load-balancer certificates have one. */
  acmKey?: string;
}

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

/**
 * The certificates one module's stack reads, with the names, domains and tags the stack's custom
 * resource passed. A `provided` certificate is the operator's, so nothing is generated for it.
 *
 * A pure function over the configuration, so the deploy and the day-zero rehearsal read one
 * declaration rather than two that can drift apart.
 */
export function certificateHooks(
  config: ClusterConfig,
  cluster: string,
  moduleInfo: ModuleInfo,
): CertificateHook[] {
  const moduleId = moduleInfo.module_id;
  const rawKmsKeyId = config.getString('cluster.secretsmanager.kms_key_id');
  const kmsKeyId = isEmpty(rawKmsKeyId) ? undefined : rawKmsKeyId;

  if (moduleInfo.name === MODULE_NAME_CLUSTER) {
    const hooks: CertificateHook[] = [];
    if (!config.getBool('cluster.load_balancers.external_alb.certificates.provided', false)) {
      hooks.push({
        request: {
          certificateName: `${cluster}-external`,
          domainName: `${cluster}.idea.default`,
          tags: { Name: `${cluster} external alb certs`, 'idea:ClusterName': cluster },
          kmsKeyId,
          importToAcm: true,
        },
        certificateKey: 'cluster.load_balancers.external_alb.certificates.certificate_secret_arn',
        privateKeyKey: 'cluster.load_balancers.external_alb.certificates.private_key_secret_arn',
        acmKey: 'cluster.load_balancers.external_alb.certificates.acm_certificate_arn',
      });
    }
    const privateHostedZoneName = config.getString('cluster.route53.private_hosted_zone_name', undefined, {
      required: true,
    }) as string;
    hooks.push({
      request: {
        certificateName: `${cluster}-internal`,
        domainName: `*.${privateHostedZoneName}`,
        tags: { Name: `${cluster} internal alb certs`, 'idea:ClusterName': cluster },
        kmsKeyId,
        importToAcm: true,
      },
      certificateKey: 'cluster.load_balancers.internal_alb.certificates.certificate_secret_arn',
      privateKeyKey: 'cluster.load_balancers.internal_alb.certificates.private_key_secret_arn',
      acmKey: 'cluster.load_balancers.internal_alb.certificates.acm_certificate_arn',
    });
    return hooks;
  }

  if (moduleInfo.name === MODULE_NAME_DIRECTORYSERVICE) {
    if (config.getString('directoryservice.provider') !== DIRECTORYSERVICE_OPENLDAP) return [];
    const hostname = config.getString('directoryservice.hostname', undefined, { required: true }) as string;
    return [
      {
        request: {
          certificateName: `${cluster}-${moduleId}`,
          domainName: hostname,
          tags: {
            Name: `${cluster}-${moduleId}`,
            'idea:ClusterName': cluster,
            'idea:ModuleName': MODULE_NAME_DIRECTORYSERVICE,
          },
          kmsKeyId,
          importToAcm: false,
        },
        certificateKey: 'directoryservice.tls_certificate_secret_arn',
        privateKeyKey: 'directoryservice.tls_private_key_secret_arn',
      },
    ];
  }

  if (moduleInfo.name === MODULE_NAME_VIRTUAL_DESKTOP_CONTROLLER) {
    const prefix = 'virtual-desktop-controller.dcv_connection_gateway.certificate';
    if (config.getBool(`${prefix}.provided`, false)) return [];
    return [
      {
        request: {
          certificateName: `${cluster}-${moduleId}-gateway-certificate`,
          domainName: `${moduleId}.${cluster}.idea.default`,
          tags: {
            Name: `${cluster}-${moduleId}-gateway Self Signed Certificate`,
            'idea:ClusterName': cluster,
            'idea:ModuleName': MODULE_NAME_VIRTUAL_DESKTOP_CONTROLLER,
          },
          kmsKeyId,
          importToAcm: false,
        },
        certificateKey: `${prefix}.certificate_secret_arn`,
        privateKeyKey: `${prefix}.private_key_secret_arn`,
      },
    ];
  }

  return [];
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
    await this.ensureCertificates(moduleInfo);
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
    if (moduleInfo.name === MODULE_NAME_CLUSTER) await this.mergeClientIps();
  }

  /**
   * The certificates this module's stack reads, with the names, domains and tags the stack's
   * custom resource passed.
   */
  certificateHooks(moduleInfo: ModuleInfo): CertificateHook[] {
    return certificateHooks(this.config, this.clusterName, moduleInfo);
  }

  /**
   * Generates or adopts this module's certificates and publishes their ARNs, before the stack that
   * reads them synthesizes. The rows go to the settings table and to the configuration this
   * process holds, so the synthesis reads the same values whether it re-reads the table or not.
   */
  private async ensureCertificates(moduleInfo: ModuleInfo): Promise<void> {
    const certificates = this.deps.certificates;
    if (certificates === undefined) return;
    const hooks = this.certificateHooks(moduleInfo);
    if (hooks.length === 0) return;

    const writer = await this.deps.configWriter({
      clusterName: this.clusterName,
      awsRegion: this.awsRegion,
      awsProfile: this.options.awsProfile,
    });
    for (const hook of hooks) {
      const result = await ensureSelfSignedCertificate(hook.request, certificates);
      this.deps.out(`certificate ${hook.request.certificateName}: ${result.certificateSecretArn}`);
      await this.publishSetting(writer, hook.certificateKey, result.certificateSecretArn);
      await this.publishSetting(writer, hook.privateKeyKey, result.privateKeySecretArn);
      if (hook.acmKey !== undefined && result.acmCertificateArn !== undefined) {
        await this.publishSetting(writer, hook.acmKey, result.acmCertificateArn);
      }
    }
  }

  /** Writes one row under the key the module's settings are scoped by, table and memory both. */
  private async publishSetting(writer: ConfigWriter, key: string, value: string): Promise<void> {
    const realKey = this.config.getRealKey(key);
    await writer.setConfigEntry(realKey, value);
    this.config.setEntry(realKey, value);
  }

  /**
   * Add the configured client addresses to the cluster prefix list. The cluster stack creates the
   * list and nothing else writes to it, so this runs once the stack has published the list id and
   * reads the settings again to get it: on a first deploy the id does not exist until then. It is
   * add-only, which is why it can run on every cluster deploy.
   */
  private async mergeClientIps(): Promise<void> {
    const api = this.deps.prefixList;
    if (api === undefined) return;
    const config = await ClusterConfig.fromDynamoDb(this.clusterName, this.awsRegion, {
      moduleSet: this.moduleSet,
      scan: this.deps.scan,
    });
    await mergeClientIpEntries({ api, config, out: this.deps.out });
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
