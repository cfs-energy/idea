/**
 * Port of `deploy` (`app_main.py:1043-1133`) and of `bootstrap`'s CLI half.
 *
 * `MODULES...` are module ids; `all` may only appear on its own. The ordering, the batching and the
 * per-module CDK invocation live in `deployment-helper.ts` and `cdk-invoker.ts`.
 */

import { writeFileSync } from 'node:fs';
import { join } from 'node:path';

import type { Command } from 'commander';

import { ClusterConfig } from '../../config/cluster-config.ts';
import { InvalidParams } from '../../config/values.ts';
import { buildBootstrapArgv, bootstrapTags, bootstrapStackName } from './bootstrap.ts';
import { CdkInvoker, cdkBin, clusterCdkDir, ExitWithCode, setupClusterCdkDir, type Deps } from '../cdk-invoker.ts';
import { DeploymentHelper } from '../deployment-helper.ts';
import { checkAwsvpcTrunking } from './upgrade.ts';

export interface DeployCommandOptions {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
  terminationProtection?: string;
  deploymentId?: string;
  upgrade?: boolean;
  forceBuildBootstrap?: boolean;
  rollback?: boolean;
  optimizeDeployment?: boolean;
  moduleSet: string;
  allowReplacement?: string[];
}

/** `deploy`'s argument handling: dedupe, and `all` only as the only module. */
export function resolveRequestedModules(modules: readonly string[]): {
  allModules: boolean;
  moduleIds: string[] | undefined;
} {
  const moduleIds: string[] = [];
  let allModules = false;
  for (const moduleId of modules) {
    if (moduleId === 'all') allModules = true;
    if (moduleIds.includes(moduleId)) continue;
    moduleIds.push(moduleId);
  }
  if (allModules) {
    if (moduleIds.length > 1) {
      throw new InvalidParams('fatal error - use of "all" deployment must be the only requested module');
    }
    return { allModules, moduleIds: undefined };
  }
  return { allModules, moduleIds };
}

export function asBoolFlag(value: string | boolean | undefined, defaultValue: boolean): boolean {
  if (value === undefined) return defaultValue;
  if (typeof value === 'boolean') return value;
  return ['true', 'yes', 'y', '1', 'on'].includes(value.toLowerCase());
}

export async function runDeploy(deps: Deps, modules: readonly string[], options: DeployCommandOptions): Promise<void> {
  const { allModules, moduleIds } = resolveRequestedModules(modules);
  const helper = await DeploymentHelper.open({
    clusterName: options.clusterName,
    awsRegion: options.awsRegion,
    moduleSet: options.moduleSet,
    awsProfile: options.awsProfile,
    terminationProtection: asBoolFlag(options.terminationProtection, true),
    deploymentId: options.deploymentId,
    upgrade: options.upgrade === true,
    allModules,
    forceBuildBootstrap: options.forceBuildBootstrap === true,
    optimizeDeployment: options.optimizeDeployment === true,
    rollback: options.rollback !== false,
    moduleIds,
    allowReplacement: options.allowReplacement ?? [],
    deps,
  });
  if (helper.getDeploymentModuleNames().includes('ecs')) {
    await checkAwsvpcTrunking(deps, options);
  }
  await helper.invoke();
}

/**
 * `bootstrap`: renders the toolkit stack template into the cluster `_cdk` directory and runs
 * `cdk bootstrap`. The rendering half is `src/cdk/stacks/bootstrap.ts`; this wires it up.
 */
export async function runBootstrap(
  deps: Deps,
  options: {
    clusterName: string;
    awsRegion: string;
    awsProfile?: string;
    terminationProtection?: string;
    customPermissionsBoundary?: string;
    cloudformationExecutionPolicies?: string;
    publicAccessBlockConfiguration?: string;
    moduleSet: string;
  },
): Promise<void> {
  const config = await ClusterConfig.fromDynamoDb(options.clusterName, options.awsRegion, {
    moduleSet: options.moduleSet,
    scan: deps.scan,
  });
  const clusterBucket = config.getString('cluster.cluster_s3_bucket', undefined, { required: true }) as string;
  const cdkHome = setupClusterCdkDir(options.clusterName, options.awsRegion);
  const templatePath = join(cdkHome, 'cdk_toolkit_stack.yml');

  // Re-rendered on every bootstrap, as `bootstrap_cluster` does: the permissions boundary and the
  // region's ELB account id are command-line and release inputs, not stack state.
  const { elbAccountIdForRegion, renderBootstrapStack } = await import('../../cdk/stacks/bootstrap.ts');
  writeFileSync(
    templatePath,
    renderBootstrapStack({
      clusterName: options.clusterName,
      awsDnsSuffix: config.getString('cluster.aws.dns_suffix', 'amazonaws.com'),
      awsElbAccountId: elbAccountIdForRegion(options.awsRegion),
      inputPermissionsBoundary: options.customPermissionsBoundary,
    }),
  );
  deps.out(
    `rendered cdk toolkit stack template for cluster: ${options.clusterName}, template: ${templatePath}`,
  );

  const invoker = new CdkInvoker({
    clusterName: options.clusterName,
    awsRegion: options.awsRegion,
    moduleId: 'bootstrap',
    moduleName: 'bootstrap',
    moduleSet: options.moduleSet,
    awsProfile: options.awsProfile,
    terminationProtection: asBoolFlag(options.terminationProtection, true),
    deps,
  });

  const { shake256Hex } = await import('../../util/shake256.ts');
  const argv = buildBootstrapArgv({
    cdkBin: cdkBin(),
    cdkAppCmd: invoker.getCdkAppCmd(),
    clusterName: options.clusterName,
    clusterBucket,
    terminationProtection: asBoolFlag(options.terminationProtection, true),
    qualifier: shake256Hex(options.clusterName, 5),
    templatePath,
    customPermissionsBoundary: options.customPermissionsBoundary,
    cloudformationExecutionPolicies: options.cloudformationExecutionPolicies,
    publicAccessBlockConfiguration: asBoolFlag(options.publicAccessBlockConfiguration, true),
    // Custom tags come from `global-settings.custom_tags`, never from a flag; the cluster tag is
    // added last so it wins a key collision.
    tags: bootstrapTags(options.clusterName, config.getList('global-settings.custom_tags', [])),
    awsProfile: options.awsProfile,
  });

  deps.out(`bootstrapping cluster CDK stack and S3 bucket: ${clusterBucket} ...`);
  deps.out(`shell> ${argv.join(' ')}`);
  const code = await deps.spawn(argv, {
    cwd: clusterCdkDir(options.clusterName, options.awsRegion),
    env: {
      ...process.env,
      AWS_DEFAULT_REGION: options.awsRegion,
      ...(options.awsProfile === undefined
        ? {}
        : {
            AWS_PROFILE: options.awsProfile,
            AWS_DEFAULT_PROFILE: options.awsProfile,
          }),
    },
  });
  if (code !== 0) throw new ExitWithCode(code);
  deps.out(`bootstrapped ${bootstrapStackName(options.clusterName)}`);
}

export function registerDeployCommands(program: Command, deps: Deps): void {
  program
    .command('bootstrap')
    .description('bootstrap cluster')
    .requiredOption('--cluster-name <cluster-name>', 'Cluster Name')
    .option('--aws-profile <aws-profile>', 'AWS Profile Name')
    .requiredOption('--aws-region <aws-region>', 'AWS Region')
    .option('--termination-protection <termination-protection>', 'Set termination protection to true or false. Default: true', 'true')
    .option('--custom-permissions-boundary <name>', 'Name of a custom permissions boundary to pass to CDK (Default to none)', '')
    .option('--cloudformation-execution-policies <policies>', 'Customize CDK CloudFormation execution policies', '')
    .option(
      '--public-access-block-configuration <public-access-block-configuration>',
      'Include S3 Block Public Access configuration for CDK staging bucket. Set to false for restricted S3 environments.',
      'true',
    )
    .option('--module-set <module-set>', 'Name of the ModuleSet. Default: default', 'default')
    .action(async (options: Parameters<typeof runBootstrap>[1]) => {
      await runBootstrap(deps, options);
    });

  program
    .command('deploy')
    .description('deploy modules. Use `all` as the module id to deploy all modules.')
    .requiredOption('--cluster-name <cluster-name>', 'Cluster Name')
    .requiredOption('--aws-region <aws-region>', 'AWS Region')
    .option('--aws-profile <aws-profile>', 'AWS Profile Name')
    .option('--termination-protection <termination-protection>', 'Set termination protection to true or false. Default: true', 'true')
    .option('--deployment-id <deployment-id>', 'A UUID to identify the deployment.')
    .option('--upgrade', 'Upgrade the module by re-running the CDK stack if the module has already been deployed.')
    .option(
      '--force-build-bootstrap',
      'If the bootstrap package directory for a given DeploymentId already exists, the directory will be deleted and rendered again.',
    )
    .option('--rollback', 'Rollback stack to stable state on failure. Default.', true)
    .option('--no-rollback', 'Do not roll back on failure, to iterate more rapidly.')
    .option('--optimize-deployment', 'Deploy applicable stacks in parallel.')
    .option('--module-set <module-set>', 'Name of the ModuleSet. Default: default', 'default')
    .option(
      '--allow-replacement <logical-id>',
      'Accept a change-set entry the deploy guard would refuse, by logical ID. Repeatable.',
      (value: string, previous: string[] = []) => [...previous, value],
    )
    .argument('<modules...>', 'module ids, or `all`')
    .action(async (modules: string[], options: DeployCommandOptions) => {
      await runDeploy(deps, modules, options);
    });
}
