/**
 * The CDK CLI entry point accepts:
 *
 *   --cluster-name X --aws-region Y --module-id Z --module-name N --deployment-id U
 *   --termination-protection true|false [--aws-profile P]
 *   [--config-file F] [--synth-reads F]
 *
 * `--config-file` and `--synth-reads` replay the cluster settings and synth-time reads from files.
 * Exactly one stack is built, then `app.synth()`.
 */

import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { parseArgs } from 'node:util';
import { pathToFileURL } from 'node:url';

import { App, Aspects, CfnDeletionPolicy, CfnResource, type Environment, type IAspect, Validations } from 'aws-cdk-lib';
import { AwsSolutionsChecks, type IApplyRule } from 'cdk-nag';
import type { IConstruct } from 'constructs';

import { ClusterConfig } from '../config/cluster-config.ts';
import { ideaVersion } from '../version.ts';
import { makeContext, type IdeaContext } from './constructs/base.ts';
import { isStatefulType } from './stateful.ts';
import { liveSynthReads, replaySynthReads, type SynthReads } from './synth-reads.ts';

export interface CdkAppOptions {
  clusterName: string;
  awsRegion: string;
  moduleId: string;
  moduleName: string;
  deploymentId: string;
  terminationProtection: boolean;
  awsProfile?: string;
  configFile?: string;
  synthReadsFile?: string;
}

/**
 * Sets `UpdateReplacePolicy: Retain` on every stateful resource in the tree.
 *
 * `DeletionPolicy` is left exactly as the stack set it. The two attributes answer different
 * questions: this one governs the old resource when an update forces a replacement, where losing
 * the data is never the intent, while `DeletionPolicy` governs a deliberate teardown, where it
 * usually is. Retaining on teardown would leave litter the delete sweep then reports as a failure.
 *
 * Neither attribute is handed to the resource provider, so adding this to a stack that is already
 * deployed changes CloudFormation's own bookkeeping and nothing about the resource itself.
 */
export class RetainStatefulOnUpdateReplace implements IAspect {
  visit(node: IConstruct): void {
    if (!CfnResource.isCfnResource(node)) return;
    if (!isStatefulType(node.cfnResourceType)) return;
    node.cfnOptions.updateReplacePolicy = CfnDeletionPolicy.RETAIN;
  }
}

/**
 * What a stack module exports: it builds itself under the app when called. A builder that needs a
 * synth-time AWS read returns a promise; `buildApp` awaits it, so the resource is in the tree
 * before `app.synth()` writes the template.
 */
export type StackBuilder = (props: StackBuildProps) => void | Promise<void>;

export interface StackBuildProps {
  app: App;
  ctx: IdeaContext;
  moduleName: string;
  deploymentId: string;
  terminationProtection: boolean;
  env: Environment;
}

export type StackRegistry = Record<string, () => Promise<StackBuilder>>;

/**
 * Module name -> the file under `stacks/` that exports `buildStack`. The specifier is built at
 * runtime so an unavailable stack module does not break type checking.
 */
const STACK_MODULES: Record<string, string> = {
  analytics: 'analytics',
  'bastion-host': 'bastion-host',
  bootstrap: 'bootstrap',
  cluster: 'cluster',
  'cluster-manager': 'cluster-manager',
  directoryservice: 'directoryservice',
  ecs: "ecs",
  'identity-provider': 'identity-provider',
  metrics: 'metrics',
  scheduler: 'scheduler',
  'shared-storage': 'shared-storage',
  'virtual-desktop-controller': 'vdc',
};

const MODULE_EXT = import.meta.url.endsWith('.ts') ? '.ts' : '.js';

export const DEFAULT_STACK_REGISTRY: StackRegistry = Object.fromEntries(
  Object.entries(STACK_MODULES).map(([moduleName, file]) => [
    moduleName,
    async () => {
      const specifier = new URL(`./stacks/${file}${MODULE_EXT}`, import.meta.url).href;
      const loaded = (await import(specifier)) as { buildStack?: StackBuilder };
      if (typeof loaded.buildStack !== 'function') {
        throw new Error(`stack module for '${moduleName}' does not export buildStack()`);
      }
      return loaded.buildStack;
    },
  ]),
);

/** `Utils.get_as_bool(value, default)` for the strings the CLI and the environment hand us. */
export function asBool(value: string | undefined, defaultValue: boolean): boolean {
  if (value === undefined) return defaultValue;
  const normalized = value.trim().toLowerCase();
  if (normalized === '') return defaultValue;
  if (['true', 'yes', 'y', '1'].includes(normalized)) return true;
  if (['false', 'no', 'n', '0'].includes(normalized)) return false;
  return defaultValue;
}

export function parseCdkAppArgs(argv: string[]): CdkAppOptions {
  const { values } = parseArgs({
    args: argv,
    options: {
      'cluster-name': { type: 'string' },
      'aws-region': { type: 'string' },
      'aws-profile': { type: 'string' },
      'module-id': { type: 'string' },
      'module-name': { type: 'string' },
      'deployment-id': { type: 'string' },
      'termination-protection': { type: 'string' },
      'config-file': { type: 'string' },
      'synth-reads': { type: 'string' },
    },
    strict: true,
    allowPositionals: false,
  });

  const required = (name: keyof typeof values): string => {
    const value = values[name];
    if (value === undefined || value === '') throw new Error(`--${String(name)} is required`);
    return value;
  };

  const options: CdkAppOptions = {
    clusterName: required('cluster-name'),
    awsRegion: required('aws-region'),
    moduleId: required('module-id'),
    moduleName: required('module-name'),
    deploymentId: required('deployment-id'),
    terminationProtection: asBool(values['termination-protection'], true),
  };
  if (values['aws-profile'] !== undefined) options.awsProfile = values['aws-profile'];
  if (values['config-file'] !== undefined) options.configFile = values['config-file'];
  if (values['synth-reads'] !== undefined) options.synthReadsFile = values['synth-reads'];
  return options;
}

/**
 * Context the CDK CLI would normally hand us in `CDK_CONTEXT_JSON`: `cdk.json`'s `context` block
 * and `cdk.context.json` from the working directory. Passed as `App({context})` **defaults**, so
 * the CLI's values still win when the app runs under it, and a standalone synth resolves
 * `Vpc.fromLookup` from the same file.
 */
export function readLocalContext(cwd: string = process.cwd()): Record<string, unknown> {
  const context: Record<string, unknown> = {};
  const cdkJson = join(cwd, 'cdk.json');
  if (existsSync(cdkJson)) {
    const parsed = JSON.parse(readFileSync(cdkJson, 'utf8')) as { context?: Record<string, unknown> };
    Object.assign(context, parsed.context ?? {});
  }
  const contextJson = join(cwd, 'cdk.context.json');
  if (existsSync(contextJson)) {
    Object.assign(context, JSON.parse(readFileSync(contextJson, 'utf8')) as Record<string, unknown>);
  }
  return context;
}

async function loadConfig(options: CdkAppOptions): Promise<ClusterConfig> {
  if (options.configFile !== undefined) {
    return ClusterConfig.fromFile(readFileSync(options.configFile, 'utf8'));
  }
  return ClusterConfig.fromDynamoDb(options.clusterName, options.awsRegion);
}

/** Builds the app and the one stack. Returns the app so a caller can synth it itself. */
/**
 * cdk-nag 3 acknowledges one finding at a time: a bare rule id no longer covers a rule's findings,
 * so `AwsSolutions-IAM5` acknowledged on a role would still report `AwsSolutions-IAM5[Resource::*]`.
 * The suppressions this tool carries are the reference's, bare by design, and are written into the
 * template as such for parity. So a bare acknowledgment on the resource or any ancestor covers every
 * finding of that rule here, which is what cdk-nag 2 did.
 */
export class IdeaSolutionsChecks extends AwsSolutionsChecks {
  protected override applyRule(params: IApplyRule): void {
    const ruleId = `${this.packName}-${params.ruleSuffixOverride ?? params.rule.name}`;
    if (bareRuleAcknowledged(params.node, ruleId)) return;
    super.applyRule(params);
  }
}

function bareRuleAcknowledged(node: IConstruct, ruleId: string): boolean {
  for (let current: IConstruct | undefined = node; current !== undefined; current = current.node.scope) {
    for (const entry of current.node.metadata) {
      if (entry.type !== Validations.ACKNOWLEDGED_RULES_METADATA_KEY) continue;
      const ids = Object.keys((entry.data ?? {}) as Record<string, unknown>).map((key) => key.replace(/^annotation::/i, ''));
      if (ids.includes(ruleId)) return true;
    }
  }
  return false;
}

export async function buildApp(
  options: CdkAppOptions,
  registry: StackRegistry = DEFAULT_STACK_REGISTRY,
): Promise<App> {
  const loadStack = registry[options.moduleName];
  if (loadStack === undefined) {
    throw new Error(
      `module not supported: '${options.moduleName}'. supported modules: ${Object.keys(registry).sort().join(', ')}`,
    );
  }

  const synthReads: SynthReads =
    options.synthReadsFile !== undefined
      ? replaySynthReads(options.synthReadsFile)
      : liveSynthReads(options.awsRegion, options.awsProfile);

  const config = await loadConfig(options);
  const ctx = makeContext({
    config,
    awsRegion: options.awsRegion,
    awsProfile: options.awsProfile,
    moduleId: options.moduleId,
    releaseVersion: ideaVersion(),
    synthReads,
  });

  // The default emits `Metadata.aws:cdk:path` for standalone synths and tests.
  const app = new App({ context: { 'aws:cdk:enable-path-metadata': true, ...readLocalContext() } });

  if (asBool(process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN, true)) {
    // cdk-nag 3 is a CDK policy validation plugin. Suppressions are written by `addNagSuppression`
    // in the cdk-nag 2 template shape the parity gate compares, so the plugin's own writer stays off.
    Validations.of(app).addPlugins(new IdeaSolutionsChecks(app));
    // Plugins have no accessor; the marker lets a test see the scan is on without synthesizing a
    // violation. App-node metadata never reaches a template.
    app.node.addMetadata('idea:cdk-nag', 'AwsSolutions');
  }

  // An upgrade must never be able to delete state. Applied to the app so every stack is covered,
  // including any added later, and applied as an aspect so it runs after the constructs have set
  // their own removal policies.
  Aspects.of(app).add(new RetainStatefulOnUpdateReplace());

  const { account } = await synthReads.callerIdentity();
  const env: Environment = { account, region: options.awsRegion };

  const buildStack = await loadStack();
  await buildStack({
    app,
    ctx,
    moduleName: options.moduleName,
    deploymentId: options.deploymentId,
    terminationProtection: options.terminationProtection,
    env,
  });

  return app;
}

/** `CdkApp.invoke`: build the one stack, then synth. */
export async function main(argv: string[] = process.argv.slice(2)): Promise<void> {
  const app = await buildApp(parseCdkAppArgs(argv));
  app.synth();
}

if (process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
