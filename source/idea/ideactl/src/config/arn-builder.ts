/**
 * Literal ARN construction from cluster config. Port of ideasdk `context/arn_builder.py`.
 *
 * Partition, region, account id and dns suffix are config values, never CDK pseudo-parameters:
 * the rendered policy documents in the live templates carry literal ARNs.
 */

import type { ClusterConfig } from './cluster-config.ts';

export interface BuildArnInput {
  partition?: string;
  service?: string;
  region?: string;
  accountId?: string;
  resource?: string;
  resourceType?: string;
  resourceId?: string;
  resourceDelimiter?: string;
}

const MODULE_CLUSTER_MANAGER = 'cluster-manager';
const MODULE_DIRECTORYSERVICE = 'directoryservice';

export class ArnBuilder {
  readonly config: ClusterConfig;

  constructor(config: ClusterConfig) {
    this.config = config;
  }

  static buildArn(input: BuildArnInput): string {
    const { partition, service, region, accountId, resource, resourceType, resourceId } = input;
    const delimiter = input.resourceDelimiter ?? '/';
    let arn = `arn:${partition}:${service}:${region}:${accountId}`;
    if (resource !== undefined) {
      arn += `:${resource}`;
    } else if (resourceType === undefined) {
      arn += `:${resourceId}`;
    } else {
      arn += `:${resourceType}${delimiter}${resourceId}`;
    }
    return arn;
  }

  getArn(service: string, resource: string, awsAccountId?: string, awsRegion?: string): string {
    return ArnBuilder.buildArn({
      partition: this.config.getString('cluster.aws.partition'),
      service,
      region: awsRegion ?? this.config.getString('cluster.aws.region'),
      accountId: awsAccountId ?? this.config.getString('cluster.aws.account_id'),
      resource,
    });
  }

  private clusterName(): string | undefined {
    return this.config.getString('cluster.cluster_name');
  }

  private region(): string | undefined {
    return this.config.getString('cluster.aws.region');
  }

  private dnsSuffix(): string | undefined {
    return this.config.getString('cluster.aws.dns_suffix');
  }

  get vpcArn(): string {
    return this.getArn('ec2', `vpc/${this.config.getString('cluster.network.vpc_id')}`);
  }

  getLogGroupArn(suffix = '*'): string {
    return this.getArn('logs', `log-group:/${this.clusterName()}${suffix}`);
  }

  getLogStreamArn(): string {
    return this.getArn('logs', `log-group:/${this.clusterName()}*:log-stream:*`);
  }

  getLambdaLogGroupArn(suffix = '*'): string {
    return this.getArn('logs', `log-group:/aws/lambda/${this.clusterName()}${suffix}`);
  }

  get lambdaLogStreamArn(): string {
    return this.getArn('logs', `log-group:/aws/lambda/${this.clusterName()}*:log-stream:*`);
  }

  get ec2CommonArns(): string[] {
    return [
      this.getArn('ec2', 'subnet/*', '*', '*'),
      this.getArn('ec2', 'key-pair/*', undefined, '*'),
      this.getArn('ec2', 'instance/*', undefined, '*'),
      this.getArn('ec2', 'snapshot/*', '*', '*'),
      this.getArn('ec2', 'launch-template/*', undefined, '*'),
      this.getArn('ec2', 'volume/*', undefined, '*'),
      this.getArn('ec2', 'security-group/*', undefined, '*'),
      this.getArn('ec2', 'placement-group/*', undefined, '*'),
      this.getArn('ec2', 'network-interface/*', undefined, '*'),
      this.getArn('ec2', 'spot-instances-request/*', '*', '*'),
      this.getArn('ec2', 'image/*', '*', '*'),
    ];
  }

  get s3GlobalArns(): string[] {
    return [
      this.getArn('s3', `dcv-license.${this.region()}/*`, '', ''),
      this.getArn('s3', 'ec2-linux-nvidia-drivers/*', '', ''),
      this.getArn('s3', 'ec2-linux-nvidia-drivers', '', ''),
      this.getArn('s3', 'ec2-windows-nvidia-drivers/*', '', ''),
      this.getArn('s3', 'ec2-windows-nvidia-drivers', '', ''),
      this.getArn('s3', 'nvidia-gaming/*', '', ''),
      this.getArn('s3', 'nvidia-gaming-drivers', '', ''),
      this.getArn('s3', 'nvidia-gaming-drivers/*', '', ''),
      this.getArn('s3', 'ec2-amd-linux-drivers/*', '', ''),
      this.getArn('s3', 'ec2-amd-linux-drivers', '', ''),
      this.getArn('s3', 'ec2-amd-windows-drivers/*', '', ''),
      this.getArn('s3', 'ec2-amd-windows-drivers', '', ''),
    ];
  }

  get albListenerRuleArn(): string {
    return this.getArn('elasticloadbalancing', `listener-rule/app/${this.clusterName()}*/*/*`);
  }

  get albListenerArn(): string {
    return this.getArn('elasticloadbalancing', `listener/app/${this.clusterName()}*/*/*`);
  }

  get targetGroupArn(): string {
    return this.getArn('elasticloadbalancing', 'targetgroup/soca*/*');
  }

  getLambdaArn(suffix = '*'): string {
    return this.getArn('lambda', `function:${this.clusterName()}-${suffix}`);
  }

  get serviceRoleArns(): string[] {
    // the service linked role path is literally 'aws-service-role' in every partition,
    // including aws-us-gov and aws-cn. only the arn prefix is partition specific.
    const partition = this.config.getString('cluster.aws.partition');
    const accountId = this.config.getString('cluster.aws.account_id');
    const roleArn = (service: string): string =>
      `arn:${partition}:iam::${accountId}:role/aws-service-role/${service}`;
    const suffix = this.dnsSuffix();
    return [
      roleArn(`s3.data-source.lustre.fsx.${suffix}/*`),
      roleArn(`autoscaling.${suffix}/*`),
      roleArn(`spotfleet.${suffix}/*`),
      roleArn(`fsx.${suffix}/*`),
    ];
  }

  /** iam path for per-project instance roles. such roles are created at runtime, not by cdk. */
  get projectRolePath(): string {
    return `/idea/${this.config.getString('cluster.cluster_name', undefined, { required: true })}/projects/`;
  }

  getProjectRoleArn(roleName = '*'): string {
    return this.getArn('iam', `role${this.projectRolePath}${roleName}`, undefined, '');
  }

  getProjectInstanceProfileArn(name = '*'): string {
    return this.getArn('iam', `instance-profile${this.projectRolePath}${name}`, undefined, '');
  }

  getProjectPolicyArn(name = '*'): string {
    return this.getArn('iam', `policy${this.projectRolePath}${name}`, undefined, '');
  }

  getProjectPermissionsBoundaryArn(): string {
    const clusterName = this.config.getString('cluster.cluster_name', undefined, { required: true });
    const awsRegion = this.config.getString('cluster.aws.region', undefined, { required: true });
    const moduleId = this.config.moduleId(MODULE_CLUSTER_MANAGER);
    return this.getArn('iam', `policy/${clusterName}-${awsRegion}-${moduleId}-project-boundary`, undefined, '');
  }

  get bedrockInvocationLogGroupName(): string {
    const clusterName = this.config.getString('cluster.cluster_name', undefined, { required: true });
    const moduleId = this.config.moduleId(MODULE_CLUSTER_MANAGER);
    return `/${clusterName}/${moduleId}/bedrock-invocations`;
  }

  get bedrockInvocationLogGroupArn(): string {
    return this.getArn('logs', `log-group:${this.bedrockInvocationLogGroupName}`);
  }

  get bedrockApplicationInferenceProfileArn(): string {
    return this.getArn('bedrock', 'application-inference-profile/*');
  }

  get bedrockSystemInferenceProfileArn(): string {
    return this.getArn('bedrock', 'inference-profile/*');
  }

  get bedrockAnySystemInferenceProfileArn(): string {
    // region and account wildcarded: a deny on system profiles has to cover every region a
    // caller could reach, not only the cluster's own.
    return this.getArn('bedrock', 'inference-profile/*', '*', '*');
  }

  get bedrockFoundationModelArn(): string {
    return this.getArn('bedrock', 'foundation-model/*', '', '*');
  }

  get sesArn(): string {
    return this.getArn('ses', 'identity/*', undefined, '*');
  }

  get dcvLicenseS3BucketArns(): string[] {
    return [this.getArn('s3', 'dcv-license.*/*', '', ''), this.getArn('s3', 'dcv-license.*', '', '')];
  }

  get s3BucketArns(): string[] {
    const bucket = this.config.getString('cluster.cluster_s3_bucket');
    return [this.getArn('s3', `${bucket}/*`, '', ''), this.getArn('s3', `${bucket}`, '', '')];
  }

  getSsmArn(resourceId: string): string {
    return ArnBuilder.buildArn({
      partition: this.config.getString('cluster.aws.partition'),
      service: 'ssm',
      region: '',
      accountId: '',
      resourceType: '',
      resourceId,
      resourceDelimiter: ':',
    });
  }

  get clusterConfigDdbArn(): string[] {
    const cluster = this.clusterName();
    return [
      this.getArn('dynamodb', `table/${cluster}.cluster-settings`, undefined, this.region()),
      this.getArn('dynamodb', `table/${cluster}.cluster-settings/stream/*`, undefined, this.region()),
      this.getArn('dynamodb', `table/${cluster}.modules`, undefined, this.region()),
    ];
  }

  getDdbTableArn(tableNameSuffix: string): string {
    return this.getArn('dynamodb', `table/${this.clusterName()}.${tableNameSuffix}`, undefined, this.region());
  }

  getAdAutomationDdbTableArn(): string {
    return this.getDdbTableArn('ad-automation');
  }

  getAdAutomationSqsQueueArn(): string {
    return this.getSqsArn(`${this.config.moduleId(MODULE_DIRECTORYSERVICE)}-ad-automation.fifo`);
  }

  getKinesisArn(): string {
    return this.getArn('kinesis', `stream/${this.clusterName()}-*`, undefined, this.region());
  }

  getSnsArn(topicNameSuffix: string): string {
    return this.getArn('sns', `${this.clusterName()}-${topicNameSuffix}`, undefined, this.region());
  }

  getSqsArn(queueNameSuffix: string): string {
    return this.getArn('sqs', `${this.clusterName()}-${queueNameSuffix}`, undefined, this.region());
  }

  getRoute53HostedzoneArn(): string {
    const partition = this.config.getString('cluster.aws.partition', undefined, { required: true });
    return `arn:${partition}:route53:::hostedzone/*`;
  }

  private kmsKeyArnFor(keyIdSetting: string): string {
    return this.getArn('kms', `key/${this.config.getString(keyIdSetting)}`, undefined, this.region());
  }

  get kmsSecretsmanagerKeyArn(): string {
    return this.kmsKeyArnFor('cluster.secretsmanager.kms_key_id');
  }

  get kmsSqsKeyArn(): string {
    return this.kmsKeyArnFor('cluster.sqs.kms_key_id');
  }

  get kmsSnsKeyArn(): string {
    return this.kmsKeyArnFor('cluster.sns.kms_key_id');
  }

  get kmsDynamodbKeyArn(): string {
    return this.kmsKeyArnFor('cluster.dynamodb.kms_key_id');
  }

  get kmsEbsKeyArn(): string {
    return this.kmsKeyArnFor('cluster.ebs.kms_key_id');
  }

  get kmsBackupKeyArn(): string {
    return this.kmsKeyArnFor('cluster.backups.backup_vault.kms_key_id');
  }

  get kmsOpensearchKeyArn(): string {
    return this.kmsKeyArnFor('analytics.opensearch.kms_key_id');
  }

  get kmsKinesisKeyArn(): string {
    return this.kmsKeyArnFor('analytics.kinesis.kms_key_id');
  }

  /** one arn per service whose kms_key_id is set; insertion order is Python's dict order. */
  get kmsKeyArn(): string[] {
    const settings = [
      'cluster.secretsmanager.kms_key_id',
      'cluster.sqs.kms_key_id',
      'cluster.sns.kms_key_id',
      'cluster.dynamodb.kms_key_id',
      'cluster.ebs.kms_key_id',
      'cluster.backups.backup_vault.kms_key_id',
      'analytics.opensearch.kms_key_id',
      'analytics.kinesis.kms_key_id',
    ];
    return settings
      .filter((setting) => this.config.getString(setting) !== undefined)
      .map((setting) => this.kmsKeyArnFor(setting));
  }

  get userPoolArn(): string {
    return this.getArn(
      'cognito-idp',
      `userpool/${this.config.getString('identity-provider.cognito.user_pool_id')}`,
      undefined,
      this.region(),
    );
  }

  getDirectoryServiceArn(): string {
    const directoryId = this.config.getString('directoryservice.directory_id', undefined, { required: true });
    return this.getArn('ds', `directory/${directoryId}`, undefined, this.region());
  }

  getDdbApplicationAutoscalingServiceRoleArn(): string {
    return this.getArn(
      'iam',
      'role/aws-service-role/dynamodb.application-autoscaling.amazonaws.com/AWSServiceRoleForApplicationAutoScaling_DynamoDBTable',
      undefined,
      '',
    );
  }
}
