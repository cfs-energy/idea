/**
 * Common CDK constructs that call the helpers from `base.ts`.
 */

import { Duration, RemovalPolicy, CustomResource as CdkCustomResource, CfnOutput } from 'aws-cdk-lib';
import type { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kinesis from 'aws-cdk-lib/aws-kinesis';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sqs from 'aws-cdk-lib/aws-sqs';

import { isEmpty } from '../../config/cluster-config.ts';
import { IdeaCodeAsset } from '../code-asset.ts';
import { renderPolicy } from '../policy.ts';
import type { IdeaContext } from './base.ts';
import {
  addCommonTags,
  addNagSuppression,
  buildServicePrincipal,
  constructId,
  kmsKeyArn,
  resourceName,
  toTitleCase,
  trimmedResourceName,
} from './base.ts';

/** `LOG_RETENTION_DAYS`, the retention values a log group accepts, mapped to the cdk enum. */
export const LOG_RETENTION_DAYS: Record<number, logs.RetentionDays> = {
  1: logs.RetentionDays.ONE_DAY,
  3: logs.RetentionDays.THREE_DAYS,
  5: logs.RetentionDays.FIVE_DAYS,
  7: logs.RetentionDays.ONE_WEEK,
  14: logs.RetentionDays.TWO_WEEKS,
  30: logs.RetentionDays.ONE_MONTH,
  60: logs.RetentionDays.TWO_MONTHS,
  90: logs.RetentionDays.THREE_MONTHS,
  120: logs.RetentionDays.FOUR_MONTHS,
  150: logs.RetentionDays.FIVE_MONTHS,
  180: logs.RetentionDays.SIX_MONTHS,
  365: logs.RetentionDays.ONE_YEAR,
  400: logs.RetentionDays.THIRTEEN_MONTHS,
  545: logs.RetentionDays.EIGHTEEN_MONTHS,
  731: logs.RetentionDays.TWO_YEARS,
  1827: logs.RetentionDays.FIVE_YEARS,
  3653: logs.RetentionDays.TEN_YEARS,
};

const MAX_NAME_LENGTH = 64;

// --- Lambda -----------------------------------------------------------------------------------

export interface LambdaFunctionProps {
  ideaCodeAsset?: IdeaCodeAsset;
  code?: lambda.Code;
  handler?: string;
  description?: string;
  memorySize?: number;
  runtime?: lambda.Runtime;
  timeoutSeconds?: number;
  vpc?: ec2.IVpc;
  securityGroups?: ec2.ISecurityGroup[];
  vpcSubnets?: ec2.SubnetSelection;
  logRetention?: logs.RetentionDays;
  logRetentionRole?: iam.IRole;
  environment?: Record<string, string>;
  role?: iam.IRole;
}

function lambdaFunctionName(ctx: IdeaContext, name: string): string {
  const functionName = resourceName(ctx, name);
  return functionName.length > MAX_NAME_LENGTH ? trimmedResourceName(ctx, name, false, MAX_NAME_LENGTH) : functionName;
}

export class LambdaFunction extends lambda.Function {
  constructor(ctx: IdeaContext, name: string, scope: Construct, props: LambdaFunctionProps = {}) {
    let code = props.code;
    let handler = props.handler;
    if (props.ideaCodeAsset !== undefined) {
      code = lambda.Code.fromAsset(props.ideaCodeAsset.assetPath());
      handler = props.ideaCodeAsset.lambdaHandler;
    } else if (code === undefined || handler === undefined) {
      throw new Error('Provide either idea_code_asset or (code and handler)');
    }

    super(scope, constructId(name), {
      functionName: lambdaFunctionName(ctx, name),
      description: props.description,
      memorySize: props.memorySize ?? 128,
      runtime: props.runtime ?? lambda.Runtime.PYTHON_3_13,
      timeout: Duration.seconds(props.timeoutSeconds ?? 60),
      logRetention: props.logRetention,
      handler,
      environment: props.environment,
      code,
      role: props.role,
      vpc: props.vpc,
      securityGroups: props.securityGroups,
      vpcSubnets: props.vpcSubnets,
      logRetentionRole: props.logRetentionRole,
    });

    addCommonTags(ctx, this, name);
    addNagSuppression(this, [
      { rule_id: 'AwsSolutions-L1', reason: 'Lambda runtime uses Python 3.13 by default.' },
    ]);
  }
}

// --- IAM --------------------------------------------------------------------------------------

export interface PolicyProps {
  policyTemplateName: string;
  vars?: Record<string, unknown>;
  moduleId?: string;
}

export class Policy extends iam.Policy {
  constructor(ctx: IdeaContext, name: string, scope: Construct, props: PolicyProps) {
    super(scope, constructId(name), {
      document: iam.PolicyDocument.fromJson(
        renderPolicy(props.policyTemplateName, {
          config: ctx.config,
          moduleId: props.moduleId,
          vars: props.vars,
        }),
      ),
    });

    addCommonTags(ctx, this, name);
    addNagSuppression(this, [
      {
        rule_id: 'AwsSolutions-IAM5',
        reason: 'Wild-card policies are scoped with conditions and/or applicable prefixes.',
      },
    ]);
  }
}

export interface ManagedPolicyProps extends PolicyProps {
  description: string;
  managedPolicyName: string;
}

export class ManagedPolicy extends iam.ManagedPolicy {
  constructor(ctx: IdeaContext, name: string, scope: Construct, props: ManagedPolicyProps) {
    super(scope, constructId(name), {
      managedPolicyName: props.managedPolicyName,
      description: props.description,
      document: iam.PolicyDocument.fromJson(
        renderPolicy(props.policyTemplateName, {
          config: ctx.config,
          moduleId: props.moduleId,
          vars: props.vars,
        }),
      ),
    });

    addCommonTags(ctx, this, name);
    addNagSuppression(this, [
      {
        rule_id: 'AwsSolutions-IAM5',
        reason:
          'AWS Managed Policies are expected to be customized and scoped down.' +
          'AWS Managed policies are copied over to enable these customizations.',
      },
    ]);
  }
}

export interface RoleProps {
  description: string;
  /** Service names, turned into `<service>.${AWS::URLSuffix}` principals. */
  assumedBy: string[];
  inlinePolicies?: iam.Policy[];
  /** Managed policy ARNs, or AWS managed policy names. */
  managedPolicies?: string[];
}

function roleName(ctx: IdeaContext, name: string): string {
  const built = resourceName(ctx, name, true);
  return built.length > MAX_NAME_LENGTH ? trimmedResourceName(ctx, name, true, MAX_NAME_LENGTH) : built;
}

export class Role extends iam.Role {
  constructor(ctx: IdeaContext, name: string, scope: Construct, props: RoleProps) {
    super(scope, constructId(name), {
      roleName: roleName(ctx, name),
      description: props.description,
      assumedBy: new iam.CompositePrincipal(...props.assumedBy.map(buildServicePrincipal)),
    });

    addCommonTags(ctx, this, name);

    for (const policy of props.inlinePolicies ?? []) {
      this.attachInlinePolicy(policy);
    }
    for (const policy of props.managedPolicies ?? []) {
      if (policy.startsWith('arn:')) {
        this.addManagedPolicy(iam.ManagedPolicy.fromManagedPolicyArn(this, policy.split('/')[1] as string, policy));
      } else {
        this.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName(policy));
      }
    }
  }
}

export class InstanceProfile extends iam.CfnInstanceProfile {
  constructor(ctx: IdeaContext, name: string, scope: Construct, roles: iam.Role[]) {
    super(scope, constructId(name), {
      instanceProfileName: resourceName(ctx, name, true),
      roles: roles.map((role) => role.roleName),
    });

    addCommonTags(ctx, this, name);
  }
}

// --- Custom resources -------------------------------------------------------------------------

export interface CustomResourceProviderProps {
  ideaCodeAsset: IdeaCodeAsset;
  policyStatements?: iam.PolicyStatement[];
  policyTemplateName?: string;
  removalPolicy?: RemovalPolicy;
  resourceType?: string;
  runtime?: lambda.Runtime;
  lambdaTimeoutSeconds?: number;
  lambdaLogRetentionRole?: iam.IRole;
}

/**
 * The policy, role, and lambda trio behind a `Custom::*` type. Construct ids are
 * `<name>-lambda-policy`, `<name>-role`, and `<name>-lambda`, all at the parent scope;
 * the function depends on the role and then the policy (CloudFormation renders `DependsOn` sorted).
 */
export class CustomResourceProvider {
  readonly ctx: IdeaContext;
  readonly name: string;
  readonly scope: Construct;
  readonly resourceType: string;
  readonly removalPolicy: RemovalPolicy | undefined;
  readonly lambdaPolicy: Policy | undefined;
  readonly lambdaRole: Role;
  readonly lambdaFunction: LambdaFunction;

  constructor(ctx: IdeaContext, name: string, scope: Construct, props: CustomResourceProviderProps) {
    this.ctx = ctx;
    this.name = name;
    this.scope = scope;
    this.removalPolicy = props.removalPolicy;

    const prefix = 'Custom::';
    const resourceType = props.resourceType as string;
    if (isEmpty(resourceType)) {
      this.resourceType = `${prefix}${toTitleCase(name)}`;
    } else {
      this.resourceType = resourceType.startsWith(prefix) ? resourceType : `${prefix}${resourceType}`;
    }

    const policyTemplateName = props.policyTemplateName as string;
    if (!isEmpty(policyTemplateName)) {
      this.lambdaPolicy = new Policy(ctx, `${name}-lambda-policy`, scope, { policyTemplateName });
    }

    this.lambdaRole = new Role(ctx, `${name}-role`, scope, {
      description: `Role for ${this.resourceType} for Cluster: ${ctx.clusterName}`,
      assumedBy: ['lambda'],
    });
    if (this.lambdaPolicy !== undefined) {
      this.lambdaRole.attachInlinePolicy(this.lambdaPolicy);
    }

    this.lambdaFunction = new LambdaFunction(ctx, `${name}-lambda`, scope, {
      ideaCodeAsset: props.ideaCodeAsset,
      description: `${this.resourceType} Lambda Function for Cluster: ${ctx.clusterName}`,
      timeoutSeconds: props.lambdaTimeoutSeconds ?? 60,
      role: this.lambdaRole,
      logRetentionRole: props.lambdaLogRetentionRole,
      runtime: props.runtime ?? lambda.Runtime.PYTHON_3_13,
    });
    addNagSuppression(this.lambdaFunction, [
      { rule_id: 'AwsSolutions-L1', reason: 'Python Runtime is selected for stability.' },
    ]);

    for (const statement of props.policyStatements ?? []) {
      this.lambdaFunction.addToRolePolicy(statement);
    }

    this.lambdaFunction.node.addDependency(this.lambdaRole);
    if (this.lambdaPolicy !== undefined) {
      this.lambdaFunction.node.addDependency(this.lambdaPolicy);
    }
  }

  invoke(name: string, properties: Record<string, unknown>): CdkCustomResource {
    const customResource = new CdkCustomResource(this.scope, name, {
      serviceToken: this.lambdaFunction.functionArn,
      properties,
      removalPolicy: this.removalPolicy,
      resourceType: this.resourceType,
    });
    customResource.node.addDependency(this.lambdaFunction);
    return customResource;
  }
}

export class CreateTagsCustomResource extends CustomResourceProvider {
  constructor(ctx: IdeaContext, scope: Construct, lambdaLogRetentionRole?: iam.IRole) {
    super(ctx, 'ec2-create-tags', scope, {
      ideaCodeAsset: new IdeaCodeAsset('idea_custom_resource_create_tags'),
      policyTemplateName: 'custom-resource-ec2-create-tags.yml',
      resourceType: 'EC2CreateTags',
      lambdaLogRetentionRole,
    });
  }

  apply(name: string, resourceId: string, tags: Record<string, unknown>): CdkCustomResource {
    const awsTags = Object.entries(tags).map(([key, value]) => ({ Key: key, Value: String(value) }));
    return this.invoke(name, { ResourceId: resourceId, Tags: awsTags });
  }
}

// --- Messaging --------------------------------------------------------------------------------

export interface SQSQueueProps {
  contentBasedDeduplication?: boolean;
  dataKeyReuse?: Duration;
  deadLetterQueue?: sqs.DeadLetterQueue;
  deduplicationScope?: sqs.DeduplicationScope;
  deliveryDelay?: Duration;
  /** Defaults to `true`. */
  encryptAtRest?: boolean;
  encryption?: sqs.QueueEncryption;
  /** A KMS key id or ARN from config, not a key object. */
  encryptionMasterKey?: string;
  fifo?: boolean;
  fifoThroughputLimit?: sqs.FifoThroughputLimit;
  maxMessageSizeBytes?: number;
  queueName?: string;
  receiveMessageWaitTime?: Duration;
  removalPolicy?: RemovalPolicy;
  retentionPeriod?: Duration;
  visibilityTimeout?: Duration;
  isDeadLetterQueue?: boolean;
}

function queueEncryption(
  ctx: IdeaContext,
  id: string,
  scope: Construct,
  props: SQSQueueProps,
): { encryption: sqs.QueueEncryption; encryptionMasterKey: kms.IKey | undefined } {
  if (props.encryptAtRest === false) {
    return { encryption: sqs.QueueEncryption.UNENCRYPTED, encryptionMasterKey: undefined };
  }
  if (!isEmpty(props.encryptionMasterKey)) {
    return {
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: kms.Key.fromKeyArn(
        scope,
        `${id}-kms-key`,
        kmsKeyArn(ctx, props.encryptionMasterKey as string),
      ),
    };
  }
  return { encryption: props.encryption ?? sqs.QueueEncryption.KMS_MANAGED, encryptionMasterKey: undefined };
}

export class SQSQueue extends sqs.Queue {
  constructor(ctx: IdeaContext, id: string, scope: Construct, props: SQSQueueProps = {}) {
    const encrypted = props.encryptAtRest !== false;
    const { encryption, encryptionMasterKey } = queueEncryption(ctx, id, scope, props);

    super(scope, constructId(id), {
      contentBasedDeduplication: props.contentBasedDeduplication,
      dataKeyReuse: props.dataKeyReuse,
      deadLetterQueue: props.deadLetterQueue,
      deduplicationScope: props.deduplicationScope,
      deliveryDelay: props.deliveryDelay,
      encryption,
      encryptionMasterKey,
      fifo: props.fifo,
      fifoThroughputLimit: props.fifoThroughputLimit,
      maxMessageSizeBytes: props.maxMessageSizeBytes,
      queueName: props.queueName,
      receiveMessageWaitTime: props.receiveMessageWaitTime,
      removalPolicy: props.removalPolicy,
      retentionPeriod: props.retentionPeriod,
      visibilityTimeout: props.visibilityTimeout,
    });

    addCommonTags(ctx, this, id);

    if (encrypted) {
      this.addToResourcePolicy(
        new iam.PolicyStatement({
          sid: 'AlwaysEncrypted',
          effect: iam.Effect.DENY,
          actions: ['sqs:*'],
          conditions: { Bool: { 'aws:SecureTransport': 'false' } },
          resources: [this.queueArn],
          principals: [new iam.AnyPrincipal()],
        }),
      );
    } else {
      addNagSuppression(this, [
        {
          rule_id: 'AwsSolutions-SQS2',
          reason: 'SQS encryption key is configurable, but is not provided in cluster config.',
        },
      ]);
      addNagSuppression(this, [
        {
          rule_id: 'AwsSolutions-SQS4',
          reason: 'SQS encryption key is configurable, but is not provided in cluster config.',
        },
      ]);
    }

    if (props.isDeadLetterQueue === true) {
      addNagSuppression(this, [{ rule_id: 'AwsSolutions-SQS3', reason: 'Dead letter queue' }]);
    }
  }
}

export interface SNSTopicProps {
  fifo?: boolean;
  /** A KMS key id or ARN from config; falls back to the `alias/aws/sns` managed key. */
  masterKey?: string;
  displayName?: string;
  topicName?: string;
  policyStatements?: iam.PolicyStatement[];
}

export class SNSTopic extends sns.Topic {
  constructor(ctx: IdeaContext, id: string, scope: Construct, props: SNSTopicProps = {}) {
    const topicName = isEmpty(props.topicName) ? resourceName(ctx, id) : (props.topicName as string);
    const displayName = isEmpty(props.displayName) ? topicName : (props.displayName as string);
    const masterKey = isEmpty(props.masterKey)
      ? kms.Alias.fromAliasName(scope, `${id}-kms-key-default`, 'alias/aws/sns')
      : kms.Key.fromKeyArn(scope, `${id}-kms-key`, kmsKeyArn(ctx, props.masterKey as string));

    super(scope, constructId(id), { displayName, fifo: props.fifo, topicName, masterKey });

    addCommonTags(ctx, this, id);

    for (const statement of props.policyStatements ?? []) {
      this.addToResourcePolicy(statement);
    }

    this.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'AlwaysEncrypted',
        effect: iam.Effect.DENY,
        actions: ['SNS:Publish'],
        conditions: { Bool: { 'aws:SecureTransport': 'false' } },
        resources: [this.topicArn],
        principals: [new iam.AnyPrincipal()],
      }),
    );
  }
}

// --- Streams and outputs ----------------------------------------------------------------------

export interface KinesisStreamProps {
  streamName: string;
  streamMode: kinesis.StreamMode;
  shardCount?: number;
  removalPolicy?: RemovalPolicy;
}

export class KinesisStream extends kinesis.Stream {
  constructor(ctx: IdeaContext, name: string, scope: Construct, props: KinesisStreamProps) {
    const kmsKeyId = ctx.config.getString('analytics.kinesis.kms_key_id');
    const encryptionKey =
      kmsKeyId !== undefined
        ? kms.Key.fromKeyArn(scope, 'kinesis-kms-key', kmsKeyArn(ctx, kmsKeyId))
        : kms.Alias.fromAliasName(scope, 'kinesis-kms-key-default', 'alias/aws/kinesis');
    super(scope, constructId(name), {
      streamName: `${ctx.clusterName}-${props.streamName}`,
      streamMode: props.streamMode,
      encryption: kinesis.StreamEncryption.KMS,
      encryptionKey,
      shardCount: props.shardCount,
      removalPolicy: props.removalPolicy,
    });

    addCommonTags(ctx, this, name);
  }
}

/** `common.py:Output`, a thin `CfnOutput` wrapper; the construct id is the name verbatim. */
export function output(
  scope: Construct,
  name: string,
  props: { value: string; description?: string; exportName?: string },
): CfnOutput {
  return new CfnOutput(scope, name, {
    value: props.value,
    description: props.description,
    exportName: props.exportName,
  });
}
