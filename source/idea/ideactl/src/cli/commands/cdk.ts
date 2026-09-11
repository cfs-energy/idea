/**
 * Port of the `cdk` sub-group (`app_main.py:1136-1226`).
 *
 * `synth` and `diff` drive the CDK CLI for one module. `cdk-app` is the `--app` re-entry the CDK
 * CLI itself runs: it builds exactly one stack and synthesizes it. It is not a command for humans.
 */

import type { Command } from 'commander';

import { CdkInvoker, type Deps } from '../cdk-invoker.ts';

interface CdkModuleOptions {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
  deploymentId?: string;
  moduleSet: string;
}

export interface CdkAppCommandOptions {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
  moduleName: string;
  moduleId: string;
  deploymentId?: string;
  terminationProtection?: string;
  configFile?: string;
  synthReads?: string;
}

/** Rebuilds the argv `src/cdk/app.ts` parses, so `--help` stays the command's own contract. */
export function cdkAppArgv(options: CdkAppCommandOptions): string[] {
  const argv = [
    '--cluster-name',
    options.clusterName,
    '--aws-region',
    options.awsRegion,
    '--module-id',
    options.moduleId,
    '--module-name',
    options.moduleName,
  ];
  if (options.deploymentId !== undefined) argv.push('--deployment-id', options.deploymentId);
  argv.push('--termination-protection', options.terminationProtection ?? 'true');
  if (options.awsProfile !== undefined) argv.push('--aws-profile', options.awsProfile);
  if (options.configFile !== undefined) argv.push('--config-file', options.configFile);
  if (options.synthReads !== undefined) argv.push('--synth-reads', options.synthReads);
  return argv;
}

export function registerCdkCommands(program: Command, deps: Deps): Command {
  const cdk = program.command('cdk').description('cdk options');

  const moduleCommand = (name: string, description: string): Command =>
    cdk
      .command(name)
      .description(description)
      .requiredOption('--cluster-name <cluster-name>', 'Cluster Name')
      .requiredOption('--aws-region <aws-region>', 'AWS Region')
      .option('--aws-profile <aws-profile>', 'AWS Profile Name')
      .option('--deployment-id <deployment-id>', 'A UUID to identify the deployment.')
      .option('--module-set <module-set>', 'Name of the ModuleSet. Default: default', 'default')
      .argument('<module>', 'module id');

  moduleCommand('synth', 'synthesize cloudformation template for a module').action(
    async (module: string, options: CdkModuleOptions) => {
      const invoker = await CdkInvoker.open({ ...options, moduleId: module, deps });
      await invoker.cdkSynth();
    },
  );

  moduleCommand('diff', 'compares the specified module with the deployed module').action(
    async (module: string, options: CdkModuleOptions) => {
      const invoker = await CdkInvoker.open({ ...options, moduleId: module, deps });
      await invoker.cdkDiff();
    },
  );

  cdk
    .command('cdk-app')
    .description('cdk app')
    .requiredOption('--cluster-name <cluster-name>', 'Cluster Name')
    .option('--aws-profile <aws-profile>', 'AWS Profile Name')
    .requiredOption('--aws-region <aws-region>', 'AWS Region')
    .requiredOption('--module-name <module-name>', 'module name')
    .requiredOption('--module-id <module-id>', 'module id')
    .option('--deployment-id <deployment-id>', 'A UUID to identify the deployment.')
    .option(
      '--termination-protection <termination-protection>',
      'Toggle termination protection for the cloud formation stack. Default: true',
      'true',
    )
    .option('--config-file <config-file>', 'Replay cluster settings from a table dump instead of DynamoDB.')
    .option('--synth-reads <synth-reads>', 'Replay the synth-time AWS reads from a file.')
    .action(async (options: CdkAppCommandOptions) => {
      const { main } = await import('../../cdk/app.ts');
      await main(cdkAppArgv(options));
    });

  return cdk;
}
