/**
 * Helpers for construct identifiers, tags, physical names, and suppressions.
 */

import type { IConstruct } from 'constructs';
import { Aws, CfnResource, Stack, Tags, Validations } from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';

import type { ClusterConfig } from '../../config/cluster-config.ts';
import type { SynthReads } from '../synth-reads.ts';
import {
  buildInstanceProfileArn,
  buildResourceName,
  buildTrimmedResourceName,
  getKmsKeyArn,
} from '../../util/names.ts';

/** `ideadatamodel.constants` tag keys used by the CDK app. */
export const IDEA_TAG_NAME = 'Name';
export const IDEA_TAG_CLUSTER_NAME = 'idea:ClusterName';
export const IDEA_TAG_MODULE_ID = 'idea:ModuleId';
export const IDEA_TAG_MODULE_NAME = 'idea:ModuleName';
export const IDEA_TAG_MODULE_VERSION = 'idea:ModuleVersion';
export const IDEA_TAG_BACKUP_PLAN = 'idea:BackupPlan';
export const IDEA_TAG_NODE_TYPE = 'idea:NodeType';

export const MODULE_CLUSTER = 'cluster';
export const METRICS_PROVIDER_AMAZON_MANAGED_PROMETHEUS = 'amazon_managed_prometheus';
export const DIRECTORYSERVICE_OPENLDAP = 'openldap';
export const DIRECTORYSERVICE_ACTIVE_DIRECTORY = 'activedirectory';
export const DIRECTORYSERVICE_AWS_MANAGED_ACTIVE_DIRECTORY = 'aws_managed_activedirectory';

export interface IdeaNagSuppression {
  rule_id: string;
  reason: string;
}

/**
 * Context built once per synth and handed to every construct.
 */
export interface IdeaContext {
  readonly config: ClusterConfig;
  /** `cluster.cluster_name`, required. */
  readonly clusterName: string;
  readonly awsRegion: string;
  readonly awsProfile: string | undefined;
  /** The module id this process is synthesizing. */
  readonly moduleId: string;
  /** `ideaadministrator.__version__`, the release stamped into descriptions and settings. */
  readonly releaseVersion: string;
  readonly synthReads: SynthReads;
}

export function makeContext(input: {
  config: ClusterConfig;
  awsRegion: string;
  awsProfile?: string;
  moduleId: string;
  releaseVersion: string;
  synthReads: SynthReads;
}): IdeaContext {
  return {
    config: input.config,
    clusterName: input.config.getString('cluster.cluster_name', undefined, { required: true }) as string,
    awsRegion: input.awsRegion,
    awsProfile: input.awsProfile,
    moduleId: input.moduleId,
    releaseVersion: input.releaseVersion,
    synthReads: input.synthReads,
  };
}

/** `SocaBaseConstruct.get_construct_id`: the name, verbatim. */
export function constructId(name: string): string {
  return name;
}

/** `Utils.to_title_case`: `-`/`_` to spaces, title case, spaces dropped. */
export function toTitleCase(value: string): string {
  return value
    .replace(/[-_]/g, ' ')
    // python str.title() upper-cases the first cased character of every run and lowers the rest
    .replace(/[A-Za-z]+/g, (word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .replace(/\s+/g, '');
}

/** `SocaBaseConstruct.build_resource_name`. */
export function resourceName(ctx: IdeaContext, name: string, regionSuffix = false): string {
  return buildResourceName(
    ctx.clusterName,
    name,
    regionSuffix ? (ctx.config.getString('cluster.aws.region', undefined, { required: true }) as string) : undefined,
  );
}

/** `SocaBaseConstruct.build_trimmed_resource_name`. */
export function trimmedResourceName(
  ctx: IdeaContext,
  name: string,
  regionSuffix = false,
  trimLength = 64,
): string {
  return buildTrimmedResourceName(
    ctx.clusterName,
    name,
    regionSuffix ? (ctx.config.getString('cluster.aws.region', undefined, { required: true }) as string) : undefined,
    trimLength,
  );
}

/** `SocaBaseConstruct.add_common_tags`: `Name` first, then `idea:ClusterName`. */
export function addCommonTags(ctx: IdeaContext, construct: IConstruct, name: string): void {
  Tags.of(construct).add(IDEA_TAG_NAME, resourceName(ctx, name));
  Tags.of(construct).add(IDEA_TAG_CLUSTER_NAME, ctx.clusterName);
}

/** `SocaBaseConstruct.add_backup_tags`. */
export function addBackupTags(ctx: IdeaContext, construct: IConstruct): void {
  Tags.of(construct).add(IDEA_TAG_BACKUP_PLAN, `${ctx.clusterName}-${MODULE_CLUSTER}`);
}

/** `SocaBaseConstruct.build_service_principal`: `<service>.${AWS::URLSuffix}`. */
export function buildServicePrincipal(serviceName: string): iam.ServicePrincipal {
  return new iam.ServicePrincipal(`${serviceName}.${Aws.URL_SUFFIX}`);
}

/** `SocaBaseConstruct.get_kms_key_arn`. */
export function kmsKeyArn(ctx: IdeaContext, keyId: string): string {
  return getKmsKeyArn(
    keyId,
    ctx.config.getString('cluster.aws.partition', undefined, { required: true }) as string,
    ctx.config.getString('cluster.aws.region', undefined, { required: true }) as string,
    ctx.config.getString('cluster.aws.account_id', undefined, { required: true }) as string,
  );
}

/** `SocaBaseConstruct.build_instance_profile_arn`. */
export function instanceProfileArn(ctx: IdeaContext, instanceProfileRef: string): string {
  return buildInstanceProfileArn(
    ctx.config.getString('cluster.aws.partition', undefined, { required: true }) as string,
    ctx.config.getString('cluster.aws.account_id', undefined, { required: true }) as string,
    instanceProfileRef,
  );
}

export function isDsActivedirectory(ctx: IdeaContext): boolean {
  const provider = ctx.config.getString('directoryservice.provider');
  return provider === DIRECTORYSERVICE_AWS_MANAGED_ACTIVE_DIRECTORY || provider === DIRECTORYSERVICE_ACTIVE_DIRECTORY;
}

interface NagRule {
  reason: string;
  id: string;
}

/** cdk-nag 2 `NagSuppressionHelper.addRulesToMetadata`: append, deduplicated by serialised rule. */
function mergeNagRules(existing: unknown, rules: readonly NagRule[]): { rules_to_suppress: NagRule[] } {
  const current = (existing as { rules_to_suppress?: NagRule[] } | undefined)?.rules_to_suppress ?? [];
  const serialised = [...current, ...rules].map((rule) => JSON.stringify(rule));
  return { rules_to_suppress: [...new Set(serialised)].map((rule) => JSON.parse(rule) as NagRule) };
}

/**
 * Records a rule for the scan only, with no `cdk_nag` metadata in the template. For a finding the
 * deployed template carries no suppression for, so parity keeps it that way, while the reason
 * still lives in code and the scan stops reporting it.
 */
export function acknowledgeForScan(construct: IConstruct, suppressions: IdeaNagSuppression[]): void {
  Validations.of(construct).acknowledge(...suppressions.map((suppression) => ({ id: suppression.rule_id, reason: suppression.reason })));
}

/**
 * `SocaBaseConstruct.add_nag_suppression`.
 *
 * cdk-nag 3 is a validation plugin that reads CDK acknowledgments and, if asked, writes them into
 * every descendant resource's metadata. The parity gate compares `Metadata.cdk_nag` with the
 * deployed templates, which cdk-nag 2 wrote onto exactly the construct's own L1 (or every
 * descendant's, or the template when given a stack). So this records the acknowledgment for the
 * plugin and writes the metadata itself, where cdk-nag 2 put it.
 */
export function addNagSuppression(
  construct: IConstruct,
  suppressions: IdeaNagSuppression[],
  applyToChildren = false,
): void {
  // `reason` before `id`: cdk-nag 2 wrote the rule that way and the parity gate compares strings.
  const rules: NagRule[] = suppressions.map((suppression) => ({ reason: suppression.reason, id: suppression.rule_id }));
  Validations.of(construct).acknowledge(...rules);
  if (Stack.isStack(construct)) {
    // `addStackSuppressions`: template-level metadata; the flag meant nested stacks there.
    const stacks = applyToChildren ? construct.node.findAll().filter((node): node is Stack => Stack.isStack(node)) : [construct];
    for (const stack of stacks) {
      const metadata = stack.templateOptions.metadata ?? {};
      metadata['cdk_nag'] = mergeNagRules(metadata['cdk_nag'], rules);
      stack.templateOptions.metadata = metadata;
    }
    return;
  }
  // `addResourceSuppressions`: the construct's L1, or every descendant's.
  for (const child of applyToChildren ? construct.node.findAll() : [construct]) {
    const l1 = child.node.defaultChild ?? child;
    if (CfnResource.isCfnResource(l1)) l1.addMetadata('cdk_nag', mergeNagRules(l1.getMetadata('cdk_nag'), rules));
  }
}
