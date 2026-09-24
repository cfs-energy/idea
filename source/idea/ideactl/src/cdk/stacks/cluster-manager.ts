/**
 * Nothing is exported and nothing is imported through CloudFormation: every cross-stack value is
 * a literal read out of the cluster config at synth time, and the stack hands its own values back
 * through the `Custom::ClusterSettings` resource at the end. The four `AWS::IAM::Policy`
 * resources take CDK's default `PolicyName`, which is the logical id, so the construct ids are
 * load bearing twice over.
 *
 * The bedrock block (managed policy, two custom resources with their lambdas, the delivery role)
 * is gated on `cluster-manager.bedrock.enabled`.
 *
 * Under the container flag the stack also runs the cluster-manager as a task on the container
 * stack's capacity. The task is built here because the application reads this module's own
 * settings rows at boot, so it must not start before this stack has written them.
 */

import { CustomResource, Duration, Fn, Tags } from 'aws-cdk-lib';
import * as asg from 'aws-cdk-lib/aws-autoscaling';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';

import { ArnBuilder } from '../../config/arn-builder.ts';
import type { StackBuildProps } from '../app.ts';
import { IdeaBaseStack } from '../base-stack.ts';
import { IdeaCodeAsset } from '../code-asset.ts';
import { IDEA_TAG_NAME, IDEA_TAG_NODE_TYPE, kmsKeyArn, acknowledgeForScan } from '../constructs/base.ts';
import {
  CustomResourceProvider,
  LOG_RETENTION_DAYS,
  ManagedPolicy,
  Policy,
  Role,
  SQSQueue,
} from '../constructs/common.ts';
import {
  STREAM_PREFIX_APPLICATION,
  addStorageMounts,
  adoptedLogDriver,
  applicationTargetGroup,
  applicationContainerSettings,
  attachApplicationFileLogs,
  buildEc2Service,
  buildExecutionRole,
  buildTaskDefinition,
  buildTaskRole,
  grantInjectedSecret,
  commonEnvironment,
  containerImage,
  dockerLabels,
  healthCheckGrace,
  roleSizing,
  serviceArn,
  type ContainerScope,
} from '../constructs/container.ts';
import { OAuthClientIdAndSecret } from '../constructs/directory-service.ts';
import {
  ExistingSocaCluster,
  lookupEbsKmsKey,
  lookupKeyPair,
} from '../constructs/existing-resources.ts';
import { WebPortalSecurityGroup } from '../constructs/network.ts';
import { buildBootstrapUserData } from '../userdata.ts';

const MODULE_CLUSTER_MANAGER = 'cluster-manager';
const NODE_TYPE_APP = 'app';
const OS_AMAZONLINUX2 = 'amazonlinux2';
const OS_AMAZONLINUX2023 = 'amazonlinux2023';
/** `constants.SQS_MAX_RECEIVE_COUNT_CLUSTER_TASKS` / `SQS_MAX_RECEIVE_COUNT_NOTIFICATIONS`. */
const SQS_MAX_RECEIVE_COUNT_CLUSTER_TASKS = 16;
const SQS_MAX_RECEIVE_COUNT_NOTIFICATIONS = 3;
/** `constants.SQS_VISIBILITY_TASKS` / `SQS_VISIBILITY_NOTIFICATIONS`. */
const SQS_VISIBILITY_SECONDS = 30;

/** `Utils.get_ec2_block_device_name`. */
export function ec2BlockDeviceName(baseOs: string): string {
  return baseOs === OS_AMAZONLINUX2 || baseOs === OS_AMAZONLINUX2023 ? '/dev/xvda' : '/dev/sda1';
}

export class ClusterManagerStack extends IdeaBaseStack {
  readonly cluster: ExistingSocaCluster;
  readonly arnBuilder: ArnBuilder;
  readonly bootstrapPackageUri: string;
  readonly userPool: cognito.IUserPool;
  private readonly ecsEnabled: boolean;
  private readonly hostsPresent: boolean;

  oauth2ClientSecret!: OAuthClientIdAndSecret;
  jwtSigningSecret!: secretsmanager.CfnSecret;
  clusterTasksSqsQueue!: SQSQueue;
  notificationsSqsQueue!: SQSQueue;
  clusterManagerRole!: Role;
  projectRoleBoundary: ManagedPolicy | undefined;
  bedrockInvocationLogGroupName: string | undefined;
  bedrockInvocationLogRole: Role | undefined;
  clusterManagerSecurityGroup!: WebPortalSecurityGroup;
  autoScalingGroup!: asg.AutoScalingGroup;
  webPortalEndpoint!: CustomResource;
  externalEndpoint!: CustomResource;
  internalEndpoint!: CustomResource;
  /** Present under the container flag: the cluster-manager task service and its target groups. */
  containerService: ecs.Ec2Service | undefined;
  private containerTargetGroups: elbv2.ApplicationTargetGroup[] = [];

  constructor(props: StackBuildProps) {
    super({
      scope: props.app,
      ctx: props.ctx,
      moduleName: props.moduleName,
      deploymentId: props.deploymentId,
      terminationProtection: props.terminationProtection,
      env: props.env,
    });

    this.bootstrapPackageUri = this.lookupBootstrapPackageUri();
    this.cluster = new ExistingSocaCluster(this.context, this.stack);
    this.arnBuilder = new ArnBuilder(this.context.config);
    this.ecsEnabled = this.context.config.getBool("ecs.enabled", false);
    // `ecs.enabled` alone routes the endpoints to the container services and deletes the hosts that
    // serve them in one change set, so nothing proves the new target before the old one is gone.
    // With `ecs.retain_existing_hosts` the same deploy routes the endpoints and keeps the hosts,
    // idle and unregistered, so a later deploy removes them once the containers are serving and
    // turning the flag off puts the hosts back in service.
    this.hostsPresent =
      !this.ecsEnabled || this.context.config.getBool("ecs.retain_existing_hosts", false);

    this.userPool = this.lookupUserPool();

    this.buildOauth2Client();
    this.buildJwtSigningSecret();
    this.buildAccessControlGroups(this.userPool);
    this.buildSqsQueues();
    this.buildIamRoles();
    this.buildProjectRoleBoundary();
    this.buildBedrockInvocationLogging();
    this.buildSecurityGroups();
    if (this.hostsPresent) this.buildAutoScalingGroup();
    this.buildContainerTargetGroups();
    this.buildEndpoints();
    this.buildContainerService();
    this.buildClusterSettings();
  }

  /** The input the shared container helpers take. */
  private get containerScope(): ContainerScope {
    return {
      ctx: this.context,
      stack: this.stack,
      vpc: this.cluster.vpc,
      privateSubnets: this.cluster.privateSubnets,
    };
  }

  /** The service name, fixed so the settings resource can publish it without a reference. */
  private get containerServiceName(): string {
    return `${this.clusterName}-${this.moduleId}`;
  }

  /**
   * The bootstrap package is not a CDK asset: the CLI uploads it and passes its location as the
   * `bootstrap_package_uri` context parameter. When absent, the deployment identifier derives
   * the package name for a standalone synth.
   */
  private lookupBootstrapPackageUri(): string {
    const fromContext: unknown = this.stack.node.tryGetContext('bootstrap_package_uri');
    if (typeof fromContext === 'string' && fromContext !== '') return fromContext;
    const bucket = this.context.config.getString('cluster.cluster_s3_bucket', undefined, {
      required: true,
    }) as string;
    return `s3://${bucket}/idea/bootstrap/bootstrap-${this.moduleId}-${this.deploymentId}.tar.gz`;
  }

  buildOauth2Client(): void {
    const resourceServer = this.userPool.addResourceServer('resource-server', {
      identifier: this.moduleId,
      scopes: [
        new cognito.ResourceServerScope({ scopeName: 'read', scopeDescription: 'Allow Read Access' }),
        new cognito.ResourceServerScope({ scopeName: 'write', scopeDescription: 'Allow Write Access' }),
      ],
    });

    const refreshTokenValidityHours = this.context.config.getInt(
      'cluster-manager.oauth2_client.refresh_token_validity_hours',
      24,
    );
    const client = this.userPool.addClient(`${this.moduleId}-client`, {
      accessTokenValidity: Duration.hours(1),
      authFlows: { adminUserPassword: true },
      generateSecret: true,
      idTokenValidity: Duration.hours(1),
      oAuth: {
        flows: { clientCredentials: true },
        scopes: [
          cognito.OAuthScope.custom(`${this.moduleId}/read`),
          cognito.OAuthScope.custom(`${this.moduleId}/write`),
        ],
      },
      refreshTokenValidity: Duration.hours(refreshTokenValidityHours),
      userPoolClientName: this.moduleId,
    });
    client.node.addDependency(resourceServer);

    // the client returns its own generated secret, so nothing has to read it back
    const clientSecret = (client.node.defaultChild as cognito.CfnUserPoolClient).attrClientSecret;

    this.oauth2ClientSecret = new OAuthClientIdAndSecret(
      this.context,
      this.moduleId,
      MODULE_CLUSTER_MANAGER,
      this.stack,
      client.userPoolClientId,
      clientSecret,
    );
  }

  /**
   * Signs the temporary file-download tokens. An L1 with no removal policy: regenerating it
   * invalidates every outstanding signed download URL.
   */
  buildJwtSigningSecret(): void {
    const kmsKeyId = this.context.config.getString('cluster.secretsmanager.kms_key_id');
    this.jwtSigningSecret = new secretsmanager.CfnSecret(
      this.stack,
      `${this.moduleId}-jwt-signing-secret`,
      {
        name: `${this.clusterName}-${this.moduleId}-jwt-signing-secret`,
        description: `JWT signing secret for ${this.moduleId} secure file downloads`,
        generateSecretString: {
          secretStringTemplate: '{}',
          generateStringKey: 'secret',
          excludeCharacters: ' "\'\\/`',
          includeSpace: false,
          passwordLength: 64,
          requireEachIncludedType: false,
        },
        kmsKeyId: kmsKeyId === undefined ? undefined : kmsKeyArn(this.context, kmsKeyId),
        tags: [
          { key: 'idea:ClusterName', value: this.clusterName },
          { key: 'idea:ModuleName', value: this.moduleId },
          { key: 'idea:SecretType', value: 'jwt-signing' },
          { key: 'idea:Purpose', value: 'file-download-authentication' },
        ],
      },
    );
    // cdk-nag raises AwsSolutions-SMG4 here (no rotation schedule). No deployed template carries a
    // suppression for it and parity keeps it that way, so the scan alone is told why.
    acknowledgeForScan(this.jwtSigningSecret, [
      { rule_id: 'AwsSolutions-SMG4', reason: 'The signing key is rotated by redeploying the module; Secrets Manager rotation would invalidate live tokens.' },
    ]);
  }

  buildSqsQueues(): void {
    const kmsKeyId = this.context.config.getString('cluster.sqs.kms_key_id');

    const clusterTasksDlq = new SQSQueue(this.context, 'cluster-tasks-sqs-queue-dlq', this.stack, {
      queueName: `${this.clusterName}-${this.moduleId}-tasks-dlq.fifo`,
      fifo: true,
      contentBasedDeduplication: true,
      encryptionMasterKey: kmsKeyId,
      isDeadLetterQueue: true,
    });
    this.clusterTasksSqsQueue = new SQSQueue(this.context, 'cluster-tasks-sqs-queue', this.stack, {
      queueName: `${this.clusterName}-${this.moduleId}-tasks.fifo`,
      fifo: true,
      contentBasedDeduplication: true,
      encryptionMasterKey: kmsKeyId,
      visibilityTimeout: Duration.seconds(SQS_VISIBILITY_SECONDS),
      deadLetterQueue: { maxReceiveCount: SQS_MAX_RECEIVE_COUNT_CLUSTER_TASKS, queue: clusterTasksDlq },
    });
    // Both queues use the module name for their `Name` tag.
    this.addCommonTags(this.clusterTasksSqsQueue);
    this.addCommonTags(clusterTasksDlq);

    const notificationsDlq = new SQSQueue(this.context, 'notifications-sqs-queue-dlq', this.stack, {
      queueName: `${this.clusterName}-${this.moduleId}-notifications-dlq.fifo`,
      fifo: true,
      contentBasedDeduplication: true,
      encryptionMasterKey: kmsKeyId,
      isDeadLetterQueue: true,
    });
    this.notificationsSqsQueue = new SQSQueue(this.context, 'notifications-sqs-queue', this.stack, {
      queueName: `${this.clusterName}-${this.moduleId}-notifications.fifo`,
      fifo: true,
      contentBasedDeduplication: true,
      encryptionMasterKey: kmsKeyId,
      visibilityTimeout: Duration.seconds(SQS_VISIBILITY_SECONDS),
      deadLetterQueue: { maxReceiveCount: SQS_MAX_RECEIVE_COUNT_NOTIFICATIONS, queue: notificationsDlq },
    });
    this.addCommonTags(this.notificationsSqsQueue);
    this.addCommonTags(notificationsDlq);
  }

  buildIamRoles(): void {
    this.clusterManagerRole = new Role(this.context, `${this.moduleId}-role`, this.stack, {
      description: 'IAM role assigned to the cluster-manager',
      assumedBy: ['ssm', 'ec2'],
      managedPolicies: this.getEc2InstanceManagedPolicies(),
    });
    this.clusterManagerRole.attachInlinePolicy(
      new Policy(this.context, 'cluster-manager-policy', this.stack, {
        policyTemplateName: 'cluster-manager.yml',
        moduleId: this.moduleId,
      }),
    );
  }

  isBedrockEnabled(): boolean {
    return this.context.config.getBool(`${this.moduleId}.bedrock.enabled`, false);
  }

  /** Permissions ceiling for the per-project instance roles cluster-manager creates at runtime. */
  buildProjectRoleBoundary(): void {
    if (!this.isBedrockEnabled()) return;

    this.projectRoleBoundary = new ManagedPolicy(this.context, 'project-role-boundary', this.stack, {
      managedPolicyName: `${this.clusterName}-${this.awsRegion}-${this.moduleId}-project-boundary`,
      description: 'Permissions boundary for IDEA per-project instance roles',
      policyTemplateName: 'project-role-boundary.yml',
      moduleId: this.moduleId,
    });

    // iam will not delete the boundary while runtime-created roles still reference it, so this
    // resource depends on it, forcing cloudformation to run the lambda that clears those refs
    // before the policy goes.
    const detachBoundaries = new CustomResourceProvider(
      this.context,
      'detach-project-boundaries',
      this.stack,
      {
        ideaCodeAsset: new IdeaCodeAsset('idea_custom_resource_detach_project_boundaries'),
        lambdaTimeoutSeconds: 300,
        policyTemplateName: 'custom-resource-detach-project-boundaries.yml',
        resourceType: 'ProjectRoleBoundaries',
      },
    ).invoke('project-role-boundaries', {
      RolePath: this.arnBuilder.projectRolePath,
      BoundaryPolicyArn: this.arnBuilder.getProjectPermissionsBoundaryArn(),
    });
    detachBoundaries.node.addDependency(this.projectRoleBoundary);
  }

  /**
   * Destination for bedrock invocation logs. The log group name is fixed, so a retain-then-
   * recreate cycle would fail on "already exists": the custom resource creates it when absent,
   * adopts it when present and never deletes it.
   */
  buildBedrockInvocationLogging(): void {
    if (!this.isBedrockEnabled()) return;

    const retentionInDays = this.context.config.getInt(
      `${this.moduleId}.bedrock.invocation_logging.log_retention_in_days`,
      30,
    );
    const validRetention = retentionInDays in LOG_RETENTION_DAYS;
    if (!validRetention) {
      console.warn(
        `invalid bedrock.invocation_logging.log_retention_in_days: ${retentionInDays}. ` +
          `valid values: ${Object.keys(LOG_RETENTION_DAYS).join(', ')}. ` +
          'leaving the retention of the log group unchanged.',
      );
    }
    this.bedrockInvocationLogGroupName = this.arnBuilder.bedrockInvocationLogGroupName;
    const ensureProperties: Record<string, unknown> = {
      LogGroupName: this.bedrockInvocationLogGroupName,
    };
    // a string, not a number: the custom resource's properties are compared as written
    if (validRetention) ensureProperties['RetentionInDays'] = String(retentionInDays);

    new CustomResourceProvider(this.context, 'ensure-bedrock-log-group', this.stack, {
      ideaCodeAsset: new IdeaCodeAsset('idea_custom_resource_ensure_log_group'),
      lambdaTimeoutSeconds: 60,
      policyTemplateName: 'custom-resource-ensure-log-group.yml',
      resourceType: 'BedrockInvocationLogGroup',
    }).invoke('bedrock-invocation-log-group', ensureProperties);

    this.bedrockInvocationLogRole = new Role(this.context, 'bedrock-invocation-logging', this.stack, {
      description: 'IAM role assumed by Amazon Bedrock to deliver model invocation logs',
      assumedBy: ['bedrock'],
    });
    // confused deputy guard: only this account's bedrock may assume the role. one service
    // principal means one trust statement, at index 0. the L2 does not model the condition.
    (this.bedrockInvocationLogRole.node.defaultChild as iam.CfnRole).addOverride(
      'Properties.AssumeRolePolicyDocument.Statement.0.Condition',
      { StringEquals: { 'aws:SourceAccount': { Ref: 'AWS::AccountId' } } },
    );
    this.bedrockInvocationLogRole.attachInlinePolicy(
      new Policy(this.context, 'bedrock-invocation-logging-policy', this.stack, {
        policyTemplateName: 'bedrock-invocation-logging.yml',
        moduleId: this.moduleId,
      }),
    );
  }

  buildSecurityGroups(): void {
    this.clusterManagerSecurityGroup = new WebPortalSecurityGroup(
      this.context,
      `${this.moduleId}-security-group`,
      this.stack,
      this.cluster.vpc,
      this.cluster.getSecurityGroup('bastion-host') as ec2.ISecurityGroup,
      this.cluster.getSecurityGroup('external-load-balancer') as ec2.ISecurityGroup,
    );
    // The rule exists to reach a host. It goes when the last host does, not when routing moves.
    if (!this.hostsPresent) this.removeBastionHostIngressRule(this.clusterManagerSecurityGroup);
  }

  private removeBastionHostIngressRule(securityGroup: ec2.SecurityGroup): void {
    const ingressRule = securityGroup.node.children.find(
      (child) =>
        child instanceof ec2.CfnSecurityGroupIngress &&
        child.description === "Allow SSH from Bastion Host",
    );
    if (ingressRule === undefined) {
      throw new Error("Cluster-manager security group has no bastion SSH ingress rule");
    }
    securityGroup.node.tryRemoveChild(ingressRule.node.id);
  }

  /**
   * The three IP target groups the container service registers with, created here because this is
   * the stack that owns the endpoints routing to them.
   */
  private buildContainerTargetGroups(): void {
    if (!this.ecsEnabled) return;
    const scope = this.containerScope;
    this.containerTargetGroups = (["e", "i", "w"] as const).map((suffix) =>
      applicationTargetGroup(scope, {
        constructId: `cm-ecs-${suffix}-target-group`,
        targetGroupName: this.getTargetGroupName(`cm-ecs-${suffix}`),
        port: 8443,
        healthCheckPath: '/healthcheck',
      }),
    );
  }

  /** The target group one endpoint forwards to: 0 external, 1 internal, 2 web portal. */
  private containerTargetGroupArn(index: number): string {
    const targetGroup = this.containerTargetGroups[index];
    if (targetGroup === undefined) throw new Error(`cluster-manager target group ${index} was not created`);
    return targetGroup.targetGroupArn;
  }

  /**
   * The cluster-manager task on the container stack's capacity.
   *
   * It starts after the three endpoints, because a service may not name a target group that has no
   * load balancer, and after this stack's settings resource, because the application reads
   * `client_id` and the rest of its own module's rows at boot. It runs in the cluster-manager host
   * security group, so the task holds the network position the host holds.
   */
  private buildContainerService(): void {
    if (!this.ecsEnabled) return;
    const scope = this.containerScope;
    const sizing = roleSizing(scope, 'cluster-manager');
    const { role: taskRole, policy: taskPolicy } = buildTaskRole(scope, {
      constructId: 'cluster-manager-task-role',
      name: `${this.moduleId}-task-role`,
      description: 'IAM role assigned to the cluster-manager ECS task',
      managedPolicyArns: this.getEc2InstanceManagedPolicies(),
      policyConstructId: 'cluster-manager-task-policy',
      policyTemplateName: 'cluster-manager.yml',
      policyModuleId: this.moduleId,
    });
    // The cost history backfill posts to the Datadog API with the daemon's key.
    const datadogSecretArn = this.context.config.getString('ecs.datadog.api_key_secret_arn');
    if (datadogSecretArn) {
      taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
        actions: ['secretsmanager:GetSecretValue'],
        resources: [datadogSecretArn],
      }));
      grantInjectedSecret(scope, taskRole, datadogSecretArn);
    }
    if (['activedirectory', 'aws_managed_activedirectory'].includes(
      this.context.config.getString('directoryservice.provider', '') as string,
    )) {
      taskRole.addToPolicy(new iam.PolicyStatement({
        actions: ['ecs:DescribeTasks'],
        resources: [this.stack.formatArn({ service: 'ecs', resource: 'task', resourceName: `${this.context.config.getString('ecs.cluster_name')}/*` })],
      }));
    }
    const executionRole = buildExecutionRole(
      scope,
      'cluster-manager-task-execution-role',
      `${this.moduleId}-task-execution-role`,
    );
    const taskDefinition = buildTaskDefinition(scope, 'cluster-manager-task-definition', {
      executionRole,
      taskRole,
    });
    const logGroupName = `/${this.clusterName}/${this.moduleId}`;
    const container = taskDefinition.addContainer('cluster-manager-container', {
      ...applicationContainerSettings('cluster-manager'),
      cpu: sizing.cpu,
      dockerLabels: dockerLabels(scope, 'cluster-manager'),
      environment: commonEnvironment(scope, {
        role: 'cluster-manager',
        moduleId: this.moduleId,
        moduleName: MODULE_CLUSTER_MANAGER,
      }),
      image: containerImage(scope),
      logging: adoptedLogDriver(scope, 'cluster-manager-log-group', logGroupName, STREAM_PREFIX_APPLICATION),
      memoryLimitMiB: sizing.memory,
    });
    container.addPortMappings({ containerPort: 8443, protocol: ecs.Protocol.TCP });
    addStorageMounts(scope, taskDefinition, container);
    attachApplicationFileLogs(scope, {
      idPrefix: 'cluster-manager',
      logGroupName,
      taskDefinition,
      container,
    });

    const service = buildEc2Service(scope, {
      constructId: 'cluster-manager-service',
      serviceName: this.containerServiceName,
      taskDefinition,
      desiredCount: sizing.desired,
      securityGroups: [this.clusterManagerSecurityGroup],
      healthCheckGracePeriod: healthCheckGrace('cluster-manager'),
      dependencies: [
        taskRole,
        taskPolicy,
        executionRole,
        this.externalEndpoint,
        this.internalEndpoint,
        this.webPortalEndpoint,
      ],
    });
    for (const targetGroup of this.containerTargetGroups) service.attachToApplicationTargetGroup(targetGroup);
    this.containerService = service;
  }

  buildAutoScalingGroup(): void {
    const config = this.context.config;
    const keyPair = lookupKeyPair(this.context, this.stack);
    const isPublic =
      config.getBool('cluster-manager.ec2.autoscaling.public', false) &&
      this.cluster.publicSubnets.length > 0;
    const baseOs = config.getString('cluster-manager.ec2.autoscaling.base_os', undefined, {
      required: true,
    }) as string;
    const instanceAmi = config.getString('cluster-manager.ec2.autoscaling.instance_ami', undefined, {
      required: true,
    }) as string;
    const instanceType = config.getString('cluster-manager.ec2.autoscaling.instance_type', undefined, {
      required: true,
    }) as string;
    const volumeSize = config.getInt('cluster-manager.ec2.autoscaling.volume_size', 200);
    const enableDetailedMonitoring = config.getBool(
      'cluster-manager.ec2.autoscaling.enable_detailed_monitoring',
      false,
    );
    const minCapacity = config.getInt('cluster-manager.ec2.autoscaling.min_capacity', 1);
    const maxCapacity = config.getInt('cluster-manager.ec2.autoscaling.max_capacity', 3);
    const cooldownMinutes = config.getInt('cluster-manager.ec2.autoscaling.cooldown_minutes', 5);
    const newInstancesProtectedFromScaleIn = config.getBool(
      'cluster-manager.ec2.autoscaling.new_instances_protected_from_scale_in',
      true,
    );
    const elbHealthcheckGraceTimeMinutes = config.getInt(
      'cluster-manager.ec2.autoscaling.elb_healthcheck.grace_time_minutes',
      15,
    );
    const scalingPolicyTargetUtilizationPercent = config.getInt(
      'cluster-manager.ec2.autoscaling.cpu_utilization_scaling_policy.target_utilization_percent',
      80,
    );
    const scalingPolicyEstimatedInstanceWarmupMinutes = config.getInt(
      'cluster-manager.ec2.autoscaling.cpu_utilization_scaling_policy.estimated_instance_warmup_minutes',
      15,
    );
    const rollingUpdateMaxBatchSize = config.getInt(
      'cluster-manager.ec2.autoscaling.rolling_update_policy.max_batch_size',
      1,
    );
    const rollingUpdateMinInstancesInService = config.getInt(
      'cluster-manager.ec2.autoscaling.rolling_update_policy.min_instances_in_service',
      1,
    );
    const rollingUpdatePauseTimeMinutes = config.getInt(
      'cluster-manager.ec2.autoscaling.rolling_update_policy.pause_time_minutes',
      15,
    );
    const metadataHttpTokens = config.getString(
      'cluster-manager.ec2.autoscaling.metadata_http_tokens',
      undefined,
      { required: true },
    ) as string;
    const httpsProxy = config.getString('cluster.network.https_proxy', '');
    const noProxy = config.getString('cluster.network.no_proxy', '');
    const proxyConfig: Record<string, string> =
      httpsProxy === '' ? {} : { http_proxy: httpsProxy, https_proxy: httpsProxy, no_proxy: noProxy };
    const ebsKmsKey = lookupEbsKmsKey(this.context, this.stack);

    const vpcSubnets: ec2.SubnetSelection = {
      subnets: isPublic ? this.cluster.publicSubnets : this.cluster.privateSubnets,
    };

    const blockDeviceName = ec2BlockDeviceName(baseOs);
    const blockDeviceTypeString = config.getString('cluster-manager.ec2.autoscaling.volume_type', 'gp3');
    const blockDeviceVolumeType =
      blockDeviceTypeString === 'gp3' ? ec2.EbsDeviceVolumeType.GP3 : ec2.EbsDeviceVolumeType.GP2;

    const userData = buildBootstrapUserData({
      awsRegion: this.awsRegion,
      bootstrapPackageUri: this.bootstrapPackageUri,
      installCommands: ['/bin/bash cluster-manager/setup.sh'],
      proxyConfig,
      baseOs,
    });

    const launchTemplate = new ec2.LaunchTemplate(this.stack, `${this.moduleId}-lt`, {
      instanceType: new ec2.InstanceType(instanceType),
      machineImage: ec2.MachineImage.genericLinux({ [this.awsRegion]: instanceAmi }),
      securityGroup: this.clusterManagerSecurityGroup,
      userData: ec2.UserData.custom(Fn.sub(userData)),
      keyPair,
      blockDevices: [
        {
          deviceName: blockDeviceName,
          volume: ec2.BlockDeviceVolume.ebs(volumeSize, {
            encrypted: true,
            kmsKey: ebsKmsKey,
            volumeType: blockDeviceVolumeType,
          }),
        },
      ],
      role: this.clusterManagerRole,
      requireImdsv2: metadataHttpTokens === 'required',
    });

    this.autoScalingGroup = new asg.AutoScalingGroup(this.stack, 'cluster-manager-asg', {
      vpc: this.cluster.vpc,
      vpcSubnets,
      autoScalingGroupName: `${this.clusterName}-${this.moduleId}-asg`,
      launchTemplate,
      // Monitoring.BASIC is the zero value, which the L2 reads as "not set" and therefore
      // accepts alongside a launch template.
      instanceMonitoring: enableDetailedMonitoring ? asg.Monitoring.DETAILED : asg.Monitoring.BASIC,
      groupMetrics: [asg.GroupMetrics.all()],
      minCapacity,
      maxCapacity,
      newInstancesProtectedFromScaleIn,
      cooldown: Duration.minutes(cooldownMinutes),
      healthChecks: asg.HealthChecks.withAdditionalChecks({
        additionalTypes: [asg.AdditionalHealthCheckType.ELB],
        gracePeriod: Duration.minutes(elbHealthcheckGraceTimeMinutes),
      }),
      updatePolicy: asg.UpdatePolicy.rollingUpdate({
        maxBatchSize: rollingUpdateMaxBatchSize,
        minInstancesInService: rollingUpdateMinInstancesInService,
        pauseTime: Duration.minutes(rollingUpdatePauseTimeMinutes),
      }),
      terminationPolicies: [asg.TerminationPolicy.DEFAULT],
    });

    this.autoScalingGroup.scaleOnCpuUtilization('cpu-utilization-scaling-policy', {
      targetUtilizationPercent: scalingPolicyTargetUtilizationPercent,
      estimatedInstanceWarmup: Duration.minutes(scalingPolicyEstimatedInstanceWarmupMinutes),
    });

    Tags.of(this.autoScalingGroup).add(IDEA_TAG_NODE_TYPE, NODE_TYPE_APP);
    Tags.of(this.autoScalingGroup).add(IDEA_TAG_NAME, `${this.clusterName}-${this.moduleId}`);
    this.autoScalingGroup.node.addDependency(this.clusterTasksSqsQueue);
    this.autoScalingGroup.node.addDependency(this.notificationsSqsQueue);

    if (!enableDetailedMonitoring) {
      this.addNagSuppression(
        [
          {
            rule_id: 'AwsSolutions-EC28',
            reason: 'detailed monitoring is a configurable option to save costs',
          },
        ],
        this.autoScalingGroup,
        true,
      );
    }

    this.addNagSuppression(
      [
        {
          rule_id: 'AwsSolutions-AS3',
          reason: 'ASG notifications scaling notifications can be managed via AWS Console',
        },
      ],
      this.autoScalingGroup,
    );
  }

  buildEndpoints(): void {
    const config = this.context.config;
    const clusterEndpointsLambdaArn = config.getString('cluster.cluster_endpoints_lambda_arn', undefined, {
      required: true,
    }) as string;
    const externalHttpsListenerArn = config.getString(
      'cluster.load_balancers.external_alb.https_listener_arn',
      undefined,
      { required: true },
    ) as string;

    // web portal endpoint: no conditions, it rewrites the external listener's default action
    const defaultTargetGroupArn = this.ecsEnabled
      ? this.containerTargetGroupArn(2)
      : new elbv2.CfnTargetGroup(this.stack, 'web-portal-target-group', {
          port: 8443,
          protocol: 'HTTPS',
          targetType: 'instance',
          vpcId: this.cluster.vpc.vpcId,
          name: this.getTargetGroupName('web-portal'),
          healthCheckPath: '/healthcheck',
        }).ref;

    this.webPortalEndpoint = new CustomResource(this.stack, 'web-portal-endpoint', {
      serviceToken: clusterEndpointsLambdaArn,
      properties: {
        endpoint_name: `${this.moduleId}-web-portal-endpoint`,
        listener_arn: externalHttpsListenerArn,
        priority: 0,
        default_action: true,
        actions: [{ Type: 'forward', TargetGroupArn: defaultTargetGroupArn }],
      },
      resourceType: 'Custom::WebPortalEndpoint',
    });

    const externalEndpointPriority = config.getInt(
      'cluster-manager.endpoints.external.priority',
      0,
      { required: true },
    );
    const externalEndpointPathPatterns = config.getList<string>(
      'cluster-manager.endpoints.external.path_patterns',
      [],
      { required: true },
    );
    const externalTargetGroupArn = this.ecsEnabled
      ? this.containerTargetGroupArn(0)
      : new elbv2.CfnTargetGroup(
          this.stack,
          `${this.moduleId}-external-target-group`,
          {
            port: 8443,
            protocol: 'HTTPS',
            targetType: 'instance',
            vpcId: this.cluster.vpc.vpcId,
            name: this.getTargetGroupName('cm-ext'),
            healthCheckPath: '/healthcheck',
          },
        ).ref;
    this.externalEndpoint = new CustomResource(this.stack, 'external-endpoint', {
      serviceToken: clusterEndpointsLambdaArn,
      properties: {
        endpoint_name: `${this.moduleId}-external-endpoint`,
        listener_arn: externalHttpsListenerArn,
        priority: externalEndpointPriority,
        conditions: [{ Field: 'path-pattern', Values: externalEndpointPathPatterns }],
        actions: [{ Type: 'forward', TargetGroupArn: externalTargetGroupArn }],
      },
      resourceType: 'Custom::ClusterManagerEndpointExternal',
    });

    const internalHttpsListenerArn = config.getString(
      'cluster.load_balancers.internal_alb.https_listener_arn',
      undefined,
      { required: true },
    ) as string;
    const internalEndpointPriority = config.getInt(
      'cluster-manager.endpoints.internal.priority',
      0,
      { required: true },
    );
    const internalEndpointPathPatterns = config.getList<string>(
      'cluster-manager.endpoints.internal.path_patterns',
      [],
      { required: true },
    );
    const internalTargetGroupArn = this.ecsEnabled
      ? this.containerTargetGroupArn(1)
      : new elbv2.CfnTargetGroup(
          this.stack,
          `${this.moduleId}-internal-target-group`,
          {
            port: 8443,
            protocol: 'HTTPS',
            targetType: 'instance',
            vpcId: this.cluster.vpc.vpcId,
            name: this.getTargetGroupName('cm-int'),
            healthCheckPath: '/healthcheck',
          },
        ).ref;
    this.internalEndpoint = new CustomResource(this.stack, 'internal-endpoint', {
      serviceToken: clusterEndpointsLambdaArn,
      properties: {
        endpoint_name: `${this.moduleId}-internal-endpoint`,
        listener_arn: internalHttpsListenerArn,
        priority: internalEndpointPriority,
        conditions: [{ Field: 'path-pattern', Values: internalEndpointPathPatterns }],
        actions: [{ Type: 'forward', TargetGroupArn: internalTargetGroupArn }],
      },
      resourceType: 'Custom::ClusterManagerEndpointInternal',
    });

    // Under the container flag these ARNs are the container target groups, which take IP targets,
    // so a retained group registers with nothing and stands idle until a rollback recreates its own.
    if (!this.ecsEnabled) {
      // registered on the L1, and in an order of their own: web-portal, internal, external
      (this.autoScalingGroup.node.defaultChild as asg.CfnAutoScalingGroup).targetGroupArns = [
        defaultTargetGroupArn,
        internalTargetGroupArn,
        externalTargetGroupArn,
      ];
    }
  }

  buildClusterSettings(): void {
    const clusterSettings: Record<string, unknown> = {
      deployment_id: this.deploymentId,
      client_id: this.oauth2ClientSecret.clientId.ref,
      client_secret: this.oauth2ClientSecret.clientSecret.ref,
      security_group_id: this.clusterManagerSecurityGroup.securityGroupId,
      iam_role_arn: this.clusterManagerRole.roleArn,
      task_queue_url: this.clusterTasksSqsQueue.queueUrl,
      task_queue_arn: this.clusterTasksSqsQueue.queueArn,
      notifications_queue_url: this.notificationsSqsQueue.queueUrl,
      notifications_queue_arn: this.notificationsSqsQueue.queueArn,
      // Literals under the container flag: the service has to start after this resource, because
      // the application reads its own rows from it, so this resource must not reference the service.
      asg_name: this.ecsEnabled ? this.containerServiceName : this.autoScalingGroup.autoScalingGroupName,
      asg_arn: this.ecsEnabled
        ? serviceArn(this.containerScope, this.containerServiceName)
        : this.autoScalingGroup.autoScalingGroupArn,
    };

    if (this.bedrockInvocationLogGroupName !== undefined) {
      clusterSettings['bedrock.invocation_log_group_name'] = this.bedrockInvocationLogGroupName;
      clusterSettings['bedrock.invocation_log_role_arn'] = (this.bedrockInvocationLogRole as Role).roleArn;
    }

    clusterSettings['jwt_signing_secret_arn'] = this.jwtSigningSecret.ref;

    const settings = this.updateClusterSettings(clusterSettings);
    // The task reads `client_id`, `client_secret` and the rest of this module's rows at boot, so
    // it must not start before they are written.
    this.containerService?.node.addDependency(settings);
  }
}

export function buildStack(props: StackBuildProps): void {
  new ClusterManagerStack(props);
}
