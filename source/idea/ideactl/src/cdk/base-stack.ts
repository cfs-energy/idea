/**
 * Owns one `Stack` that module stacks add constructs under. The stack name and
 * construct id are `${cluster}-${moduleId}`, which is the root of every
 * `aws:cdk:path` and feeds `Names.uniqueId` for imported peers.
 */

import { CustomResource, DefaultStackSynthesizer, Stack, type Environment } from 'aws-cdk-lib';
import type { Construct, IConstruct } from 'constructs';
import * as cognito from 'aws-cdk-lib/aws-cognito';

import { shake256Hex } from '../util/shake256.ts';
import { getTargetGroupName } from '../util/names.ts';
import type { IdeaContext, IdeaNagSuppression } from './constructs/base.ts';
import {
  IDEA_TAG_CLUSTER_NAME,
  IDEA_TAG_MODULE_ID,
  IDEA_TAG_MODULE_NAME,
  IDEA_TAG_MODULE_VERSION,
  METRICS_PROVIDER_AMAZON_MANAGED_PROMETHEUS,
  addBackupTags,
  addCommonTags,
  addNagSuppression,
  resourceName,
} from './constructs/base.ts';

export interface IdeaBaseStackProps {
  scope: Construct;
  ctx: IdeaContext;
  /** The module *name* (`virtual-desktop-controller`), not the id, it becomes `idea:ModuleName`. */
  moduleName: string;
  deploymentId: string;
  terminationProtection: boolean;
  env: Environment;
}

/** Converts `Key=k,Value=v` strings to a record. */
export function convertCustomTags(customTags: string[]): Record<string, string> {
  const result: Record<string, string> = {};
  for (const customTag of customTags) {
    const commaIndex = customTag.indexOf(',');
    if (commaIndex < 0) continue;
    const keyToken = customTag.slice(0, commaIndex);
    const valueToken = customTag.slice(commaIndex + 1);
    const keyParts = keyToken.split('Key=');
    const valueParts = valueToken.split('Value=');
    if (keyParts.length < 2 || valueParts.length < 2) continue;
    const key = (keyParts[1] as string).trim();
    const value = (valueParts[1] as string).trim();
    if (key === '' || value === '') continue;
    result[key] = value;
  }
  return result;
}

export class IdeaBaseStack {
  readonly context: IdeaContext;
  readonly stack: Stack;
  readonly stackName: string;
  readonly clusterName: string;
  readonly moduleId: string;
  readonly moduleName: string;
  readonly awsRegion: string;
  readonly deploymentId: string;
  readonly releaseVersion: string;

  constructor(props: IdeaBaseStackProps) {
    const ctx = props.ctx;
    this.context = ctx;
    this.clusterName = ctx.clusterName;
    this.moduleId = ctx.moduleId;
    this.moduleName = props.moduleName;
    this.awsRegion = ctx.awsRegion;
    this.deploymentId = props.deploymentId;
    this.releaseVersion = ctx.releaseVersion;
    this.stackName = `${this.clusterName}-${this.moduleId}`;

    // Custom tags come first. IDEA tags win a key collision.
    const tags: Record<string, string> = {
      ...convertCustomTags(ctx.config.getList<string>('global-settings.custom_tags', [])),
      [IDEA_TAG_MODULE_ID]: this.moduleId,
      [IDEA_TAG_MODULE_NAME]: this.moduleName,
      [IDEA_TAG_MODULE_VERSION]: this.releaseVersion,
      [IDEA_TAG_CLUSTER_NAME]: this.clusterName,
    };

    this.stack = new Stack(props.scope, this.stackName, {
      description: `ModuleId: ${this.moduleId}, Cluster: ${this.clusterName}, Version: ${this.releaseVersion}`,
      env: props.env,
      stackName: this.stackName,
      tags,
      terminationProtection: props.terminationProtection,
      synthesizer: new DefaultStackSynthesizer({
        qualifier: shake256Hex(this.clusterName, 5),
        bucketPrefix: 'cdk/',
        fileAssetsBucketName: ctx.config.getString('cluster.cluster_s3_bucket', undefined, {
          required: true,
        }) as string,
      }),
    });
  }

  /** Builds a resource name with the module id. */
  buildResourceName(name: string, regionSuffix = false): string {
    return resourceName(this.context, name, regionSuffix);
  }

  /**
   * Adds common tags with the module id as the name. The prefix list and EC2
   * state-change topic use `Name=<cluster>-<moduleId>`.
   */
  addCommonTags(construct: IConstruct): void {
    addCommonTags(this.context, construct, this.moduleId);
  }

  addBackupTags(construct: IConstruct): void {
    addBackupTags(this.context, construct);
  }

  addNagSuppression(suppressions: IdeaNagSuppression[], construct: IConstruct, applyToChildren = false): void {
    addNagSuppression(construct, suppressions, applyToChildren);
  }

  /** Builds a target group name and throws over 32 characters. */
  getTargetGroupName(identifier: string): string {
    return getTargetGroupName(this.clusterName, this.moduleId, identifier);
  }

  /**
   * Adds the `Custom::ClusterSettings` resource that ends every module stack.
   * Its id is `${cluster}-${moduleId}-settings`.
   */
  updateClusterSettings(clusterSettings: Record<string, unknown>): CustomResource {
    const serviceToken = this.context.config.getString('cluster.cluster_settings_lambda_arn', undefined, {
      required: true,
    }) as string;
    return new CustomResource(this.stack, `${this.clusterName}-${this.moduleId}-settings`, {
      serviceToken,
      properties: {
        cluster_name: this.clusterName,
        module_id: this.moduleId,
        version: this.releaseVersion,
        settings: clusterSettings,
      },
      resourceType: 'Custom::ClusterSettings',
    });
  }

  isMetricsProviderAmazonManagedPrometheus(): boolean {
    const provider = this.context.config.getString('metrics.provider');
    if (provider === undefined || provider === '') return false;
    return provider === METRICS_PROVIDER_AMAZON_MANAGED_PROMETHEUS;
  }

  /** Returns the managed policies for EC2 instances. */
  getEc2InstanceManagedPolicies(): string[] {
    const config = this.context.config;
    const policies: string[] = [
      config.getString('cluster.iam.policies.amazon_ssm_managed_instance_core_arn', undefined, {
        required: true,
      }) as string,
      // Logs always go to CloudWatch, regardless of the metrics provider.
      config.getString('cluster.iam.policies.cloud_watch_agent_server_arn', undefined, {
        required: true,
      }) as string,
    ];
    if (this.isMetricsProviderAmazonManagedPrometheus()) {
      policies.push(
        config.getString('cluster.iam.policies.amazon_prometheus_remote_write_arn', undefined, {
          required: true,
        }) as string,
      );
    }
    return [...policies, ...config.getList<string>('cluster.iam.ec2_managed_policy_arns', [])];
  }

  /** Looks up the user pool at construct id `${cluster}-user-pool`. */
  lookupUserPool(): cognito.IUserPool {
    return cognito.UserPool.fromUserPoolId(
      this.stack,
      `${this.clusterName}-user-pool`,
      this.context.config.getString('identity-provider.cognito.user_pool_id', undefined, {
        required: true,
      }) as string,
    );
  }

  /** Adds module administrators and users groups. */
  buildAccessControlGroups(userPool: cognito.IUserPool): void {
    new cognito.CfnUserPoolGroup(this.stack, `${this.moduleId}-administrators-group`, {
      description: `Module administrators group for module id: ${this.moduleId}, cluster: ${this.clusterName}`,
      groupName: `${this.moduleId}-administrators-module-group`,
      precedence: 3,
      userPoolId: userPool.userPoolId,
    });
    new cognito.CfnUserPoolGroup(this.stack, `${this.moduleId}-users-group`, {
      description: `Module user group for module id: ${this.moduleId}, cluster: ${this.clusterName}`,
      groupName: `${this.moduleId}-users-module-group`,
      precedence: 4,
      userPoolId: userPool.userPoolId,
    });
  }
}
