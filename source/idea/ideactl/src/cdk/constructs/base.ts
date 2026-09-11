/**
 * Helpers for construct identifiers, tags, physical names, and suppressions.
 */

import type { IConstruct } from 'constructs';
import { Aws, Stack, Tags } from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { NagSuppressions } from 'cdk-nag';

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

/** `SocaBaseConstruct.add_nag_suppression`. */
export function addNagSuppression(
  construct: IConstruct,
  suppressions: IdeaNagSuppression[],
  applyToChildren = false,
): void {
  const rules = suppressions.map((suppression) => ({ id: suppression.rule_id, reason: suppression.reason }));
  if (Stack.isStack(construct)) {
    NagSuppressions.addStackSuppressions(construct, rules, applyToChildren);
  } else {
    NagSuppressions.addResourceSuppressions(construct, rules, applyToChildren);
  }
}
