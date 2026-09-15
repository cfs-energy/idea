/**
 * Everything the stack references from another module is a literal read out of the cluster
 * config at synth time: there are no exports, no `Fn::ImportValue` and no outputs. The three
 * IAM policies take CDK's default `PolicyName`, which is the logical id, so the construct ids
 * here are load bearing twice over.
 *
 * Under the container flag the stack also runs the scheduler as a task on the container stack's
 * capacity, with its PBS state on an elastic file system this stack owns. The task is built here
 * because the application reads this module's own settings rows at boot, so it must not start
 * before this stack has written them.
 */

import { Aws, CustomResource, Duration, Fn, RemovalPolicy, Tags } from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as efs from 'aws-cdk-lib/aws-efs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as sqs from 'aws-cdk-lib/aws-sqs';

import { ArnBuilder } from '../../config/arn-builder.ts';
import type { ClusterConfig } from '../../config/cluster-config.ts';
import type { StackBuildProps } from '../app.ts';
import { IdeaBaseStack } from '../base-stack.ts';
import { IDEA_TAG_CLUSTER_NAME, IDEA_TAG_MODULE_ID, IDEA_TAG_MODULE_NAME, IDEA_TAG_NODE_TYPE } from '../constructs/base.ts';
import { InstanceProfile, Policy, Role, SQSQueue } from '../constructs/common.ts';
import {
  STREAM_PREFIX_APPLICATION,
  STREAM_PREFIX_OPENPBS,
  addStorageMounts,
  adoptedLogDriver,
  addLogTailContainer,
  applicationTargetGroup,
  attachApplicationFileLogs,
  buildEc2Service,
  buildExecutionRole,
  buildTaskDefinition,
  buildTaskRole,
  commonEnvironment,
  containerImage,
  dockerLabels,
  healthCheckGrace,
  roleSizing,
  taskStartAllowance,
  type ContainerScope,
} from '../constructs/container.ts';
import { OAuthClientIdAndSecret } from '../constructs/directory-service.ts';
import {
  ExistingSocaCluster,
  lookupClusterDns,
  lookupEbsKmsKey,
  lookupKeyPair,
} from '../constructs/existing-resources.ts';
import { ComputeNodeSecurityGroup, SchedulerSecurityGroup } from '../constructs/network.ts';
import { buildBootstrapUserData } from '../userdata.ts';

const MODULE_SCHEDULER = 'scheduler';
const MODULE_CLUSTER_MANAGER = 'cluster-manager';
/** PBS state and logs live on the scheduler task volume. */
const SCHEDULER_PBS_HOME = '/var/spool/pbs';
const NODE_TYPE_APP = 'app';
const OS_AMAZONLINUX2 = 'amazonlinux2';
const OS_AMAZONLINUX2023 = 'amazonlinux2023';
/** `constants.SQS_MAX_RECEIVE_COUNT_SCHEDULER_JOB_STATUS`. */
const SQS_MAX_RECEIVE_COUNT_SCHEDULER_JOB_STATUS = 10;

/**
 * `get_int(key, required=True)` and `get_list(key, required=True)`. Unlike `getString`, these two
 * getters have no overload for an undefined default, so the call is typed here instead.
 */
type RequiredGet<T> = (key: string, defaultValue?: T, options?: { required?: boolean }) => T;

function requiredInt(config: ClusterConfig, key: string): number {
  return (config.getInt as RequiredGet<number>)(key, undefined, { required: true });
}

function requiredList(config: ClusterConfig, key: string): string[] {
  return (config.getList as RequiredGet<string[]>)(key, undefined, { required: true });
}

/** `Utils.get_ec2_block_device_name`. */
export function ec2BlockDeviceName(baseOs: string): string {
  return baseOs === OS_AMAZONLINUX2 || baseOs === OS_AMAZONLINUX2023 ? '/dev/xvda' : '/dev/sda1';
}

export class SchedulerStack extends IdeaBaseStack {
  readonly cluster: ExistingSocaCluster;
  readonly arnBuilder: ArnBuilder;
  readonly bootstrapPackageUri: string;
  readonly userPool: cognito.IUserPool;
  private readonly ecsEnabled: boolean;
  private readonly hostsPresent: boolean;

  oauth2ClientSecret!: OAuthClientIdAndSecret;
  schedulerRole!: Role;
  schedulerInstanceProfile!: InstanceProfile;
  computeNodeRole!: Role;
  computeNodeInstanceProfile!: InstanceProfile;
  spotFleetRequestRole!: Role;
  schedulerSecurityGroup!: SchedulerSecurityGroup;
  computeNodeSecurityGroup!: ComputeNodeSecurityGroup;
  jobStatusSqsQueue!: SQSQueue;
  ec2Instance!: ec2.CfnInstance;
  clusterDnsRecordSet!: route53.RecordSet;
  externalEndpoint!: CustomResource;
  internalEndpoint!: CustomResource;
  /** Present under the container flag: the scheduler task service and its target groups. */
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
    // `ecs.enabled` alone routes the endpoints to the container service and deletes the instance
    // that serves them in one change set, so nothing proves the new target before the old one is
    // gone. With `ecs.retain_existing_hosts` the same deploy routes the endpoints and keeps the
    // instance, idle and unregistered, so a later deploy removes it once the container is serving
    // and turning the flag off puts the instance back in service.
    this.hostsPresent =
      !this.ecsEnabled || this.context.config.getBool("ecs.retain_existing_hosts", false);

    this.userPool = this.lookupUserPool();

    this.buildOauth2Client();
    this.buildAccessControlGroups(this.userPool);
    this.buildSqsQueue();
    this.buildIamRoles();
    this.buildSecurityGroups();
    if (this.hostsPresent) this.buildEc2Instance();
    // The container scheduler upserts this record itself, so the stack stops managing it as soon as
    // routing moves. An earlier retain-only deploy is what keeps the name alive across that handover.
    if (!this.ecsEnabled) this.buildRoute53RecordSet();
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
   *
   * The naming rule is local so stack synthesis does not load an S3 client.
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

    const client = this.userPool.addClient(`${this.moduleId}-client`, {
      accessTokenValidity: Duration.hours(1),
      generateSecret: true,
      idTokenValidity: Duration.hours(1),
      oAuth: {
        flows: { clientCredentials: true },
        scopes: [
          cognito.OAuthScope.custom(`${this.moduleId}/read`),
          cognito.OAuthScope.custom(`${this.moduleId}/write`),
          cognito.OAuthScope.custom(`${this.context.config.moduleId(MODULE_CLUSTER_MANAGER)}/read`),
        ],
      },
      refreshTokenValidity: Duration.days(30),
      userPoolClientName: this.moduleId,
    });
    client.node.addDependency(resourceServer);

    // the client returns its own generated secret, so nothing has to read it back
    const clientSecret = (client.node.defaultChild as cognito.CfnUserPoolClient).attrClientSecret;

    this.oauth2ClientSecret = new OAuthClientIdAndSecret(
      this.context,
      this.moduleId,
      MODULE_SCHEDULER,
      this.stack,
      client.userPoolClientId,
      clientSecret,
    );
  }

  buildIamRoles(): void {
    const ec2ManagedPolicies = this.getEc2InstanceManagedPolicies();

    // Deduplication preserves first-seen order.
    // These two lists are deduplicated in first-seen order. The reference implementation
    // deduplicated through an unordered set of strings, so the array order it emitted is a per
    // process permutation: no seed reproduces all three captured templates, and the two real
    // clusters carry the opposite order to the development one. IAM attaches a set, so a
    // permutation changes no permission and replaces nothing, but it is an ordered array in the
    // template and a comparison that reads order sees it. This is the only place the reference did
    // that, which is why only these two roles differ.
    const schedulerPolicyArns = [
      ...new Set([
        ...this.context.config.getList<string>('cluster.iam.scheduler_iam_policy_arns', []),
        ...ec2ManagedPolicies,
      ]),
    ];
    this.schedulerRole = new Role(this.context, `${this.moduleId}-role`, this.stack, {
      description: 'IAM role assigned to the scheduler',
      assumedBy: ['ssm', 'ec2'],
      managedPolicies: schedulerPolicyArns,
    });
    if (this.hostsPresent) {
      this.schedulerInstanceProfile = new InstanceProfile(
        this.context,
        `${this.moduleId}-scheduler-instance-profile`,
        this.stack,
        [this.schedulerRole],
      );
    }

    const computeNodePolicyArns = [
      ...new Set([
        ...this.context.config.getList<string>('cluster.iam.compute_node_iam_policy_arns', []),
        ...ec2ManagedPolicies,
      ]),
    ];
    this.computeNodeRole = new Role(this.context, `${this.moduleId}-compute-node-role`, this.stack, {
      description: 'IAM role assigned to the compute nodes',
      assumedBy: ['ssm', 'ec2'],
      managedPolicies: computeNodePolicyArns,
    });
    this.computeNodeInstanceProfile = new InstanceProfile(
      this.context,
      `${this.moduleId}-compute-node-instance-profile`,
      this.stack,
      [this.computeNodeRole],
    );

    this.spotFleetRequestRole = new Role(this.context, `${this.moduleId}-spot-fleet-request-role`, this.stack, {
      description: 'IAM role to manage SpotFleet requests',
      assumedBy: ['spotfleet'],
    });

    const vars = {
      scheduler_role_arn: this.schedulerRole.roleArn,
      compute_node_role_arn: this.computeNodeRole.roleArn,
      spot_fleet_request_role_arn: this.spotFleetRequestRole.roleArn,
    };

    this.schedulerRole.attachInlinePolicy(
      new Policy(this.context, 'scheduler-policy', this.stack, {
        policyTemplateName: 'scheduler.yml',
        vars,
        moduleId: this.moduleId,
      }),
    );
    this.computeNodeRole.attachInlinePolicy(
      new Policy(this.context, 'compute-node-policy', this.stack, {
        policyTemplateName: 'compute-node.yml',
        vars,
        moduleId: this.moduleId,
      }),
    );
    this.spotFleetRequestRole.attachInlinePolicy(
      new Policy(this.context, 'spot-fleet-policy', this.stack, {
        policyTemplateName: 'spot-fleet-request.yml',
        vars,
        moduleId: this.moduleId,
      }),
    );
  }

  /**
   * Compute nodes run under project roles only when the cluster integration and the scheduler
   * opt-in are both on. Read at synth time, so the grants that depend on it exist only in a
   * deployment that asked for them.
   */
  isBedrockEnabledForJobs(): boolean {
    const clusterManagerModuleId = this.context.config.moduleId(MODULE_CLUSTER_MANAGER);
    return (
      this.context.config.getBool(`${clusterManagerModuleId}.bedrock.enabled`, false) &&
      this.context.config.getBool(`${this.moduleId}.bedrock.enabled`, false)
    );
  }

  buildSqsQueue(): void {
    const kmsKeyId = this.context.config.getString('cluster.sqs.kms_key_id');

    // The dead-letter queue is created before the queue that references it.
    const deadLetterQueue = new SQSQueue(this.context, 'job-status-events-dlq', this.stack, {
      queueName: `${this.clusterName}-${this.moduleId}-job-status-events-dlq`,
      encryptionMasterKey: kmsKeyId,
      isDeadLetterQueue: true,
    });
    this.jobStatusSqsQueue = new SQSQueue(this.context, 'job-status-events', this.stack, {
      queueName: `${this.clusterName}-${this.moduleId}-job-status-events`,
      encryptionMasterKey: kmsKeyId,
      deadLetterQueue: {
        maxReceiveCount: SQS_MAX_RECEIVE_COUNT_SCHEDULER_JOB_STATUS,
        queue: deadLetterQueue,
      } satisfies sqs.DeadLetterQueue,
    });
    // Re-tags `Name` from the queue id to the module id: the last write wins.
    this.addCommonTags(this.jobStatusSqsQueue);
    this.addCommonTags(deadLetterQueue);

    if (this.isBedrockEnabledForJobs()) {
      // Bedrock project-role compute nodes carry the dcv host policy, not the compute-node
      // policy, so the execution hooks' job-status send needs its own grant, scoped to
      // per-project IAM roles.
      this.jobStatusSqsQueue.addToResourcePolicy(
        new iam.PolicyStatement({
          sid: 'ProjectRoleJobStatusEvents',
          effect: iam.Effect.ALLOW,
          actions: ['sqs:SendMessage'],
          resources: [this.jobStatusSqsQueue.queueArn],
          principals: [new iam.AnyPrincipal()],
          conditions: { ArnLike: { 'aws:PrincipalArn': this.arnBuilder.getProjectRoleArn() } },
        }),
      );
    }
  }

  buildSecurityGroups(): void {
    this.schedulerSecurityGroup = new SchedulerSecurityGroup(
      this.context,
      `${this.moduleId}-security-group`,
      this.stack,
      this.cluster.vpc,
      this.cluster.getSecurityGroup('bastion-host') as ec2.ISecurityGroup,
      this.cluster.getSecurityGroup('external-load-balancer') as ec2.ISecurityGroup,
    );
    // The rule exists to reach a host. It goes when the last host does, not when routing moves.
    if (!this.hostsPresent) this.removeBastionHostIngressRule(this.schedulerSecurityGroup);

    this.computeNodeSecurityGroup = new ComputeNodeSecurityGroup(
      this.context,
      `${this.moduleId}-compute-node-security-group`,
      this.stack,
      this.cluster.vpc,
    );
  }

  private removeBastionHostIngressRule(securityGroup: ec2.SecurityGroup): void {
    const ingressRule = securityGroup.node.children.find(
      (child) =>
        child instanceof ec2.CfnSecurityGroupIngress &&
        child.description === "Allow SSH from Bastion Host",
    );
    if (ingressRule === undefined) {
      throw new Error("Scheduler security group has no bastion SSH ingress rule");
    }
    securityGroup.node.tryRemoveChild(ingressRule.node.id);
  }

  /**
   * The two IP target groups the container service registers with, created here because this is
   * the stack that owns the endpoints routing to them.
   *
   * The fifteen-second deregistration delay is the scheduler's alone: its replacement stops the
   * running task before the new one starts, so the wait before the old target is removed is time
   * the batch server is down rather than time a draining connection gets to finish.
   */
  private buildContainerTargetGroups(): void {
    if (!this.ecsEnabled) return;
    const scope = this.containerScope;
    this.containerTargetGroups = (['e', 'i'] as const).map((suffix) =>
      applicationTargetGroup(scope, {
        constructId: `sched-ecs-${suffix}-target-group`,
        targetGroupName: this.getTargetGroupName(`sched-ecs-${suffix}`),
        port: 8443,
        healthCheckPath: '/healthcheck',
        deregistrationDelaySeconds: 15,
      }),
    );
  }

  /** The target group one endpoint forwards to: 0 external, 1 internal. */
  private containerTargetGroupArn(index: number): string {
    const targetGroup = this.containerTargetGroups[index];
    if (targetGroup === undefined) throw new Error(`scheduler target group ${index} was not created`);
    return targetGroup.targetGroupArn;
  }

  /**
   * The scheduler task on the container stack's capacity.
   *
   * The batch server holds a single-writer lock on its state directory, so two scheduler tasks
   * cannot run at once: the second to start fails to take the lock. A maximum of one hundred
   * leaves no room for a replacement to start before the running task stops, which is why this one
   * service differs from the other four, and it is also why the task carries no distinct-instance
   * placement. The cost is that a replacement is a short batch server outage rather than a
   * handover: running jobs survive, submissions fail while it is down.
   */
  private buildContainerService(): void {
    if (!this.ecsEnabled) return;
    const scope = this.containerScope;
    const sizing = roleSizing(scope, 'scheduler');
    const { role: taskRole, policy: taskPolicy } = buildTaskRole(scope, {
      constructId: 'scheduler-task-role',
      name: `${this.moduleId}-task-role`,
      description: 'IAM role assigned to the scheduler ECS task',
      managedPolicyArns: this.getEc2InstanceManagedPolicies(),
      policyConstructId: 'scheduler-task-policy',
      policyTemplateName: 'scheduler.yml',
      policyModuleId: this.moduleId,
      // The template names the roles the task may pass to a compute node or a spot fleet request.
      // Both belong to this stack, so they are references rather than names rebuilt by rule.
      policyVars: (role) => ({
        compute_node_role_arn: this.computeNodeRole.roleArn,
        scheduler_role_arn: role.roleArn,
        spot_fleet_request_role_arn: this.spotFleetRequestRole.roleArn,
      }),
    });
    const executionRole = buildExecutionRole(
      scope,
      'scheduler-task-execution-role',
      `${this.moduleId}-task-execution-role`,
    );
    const taskDefinition = buildTaskDefinition(scope, 'scheduler-task-definition', {
      executionRole,
      taskRole,
    });
    const logGroupName = `/${this.clusterName}/${this.moduleId}`;
    const container = taskDefinition.addContainer('scheduler-container', {
      cpu: sizing.cpu,
      dockerLabels: dockerLabels(scope, 'scheduler'),
      environment: {
        ...commonEnvironment(scope, {
          role: 'scheduler',
          moduleId: this.moduleId,
          moduleName: MODULE_SCHEDULER,
        }),
        IDEA_ROUTE53_ZONE_ID: this.context.config.getString(
          'cluster.route53.private_hosted_zone_id',
          undefined,
          { required: true },
        ) as string,
        IDEA_SCHEDULER_DNS_NAME: `scheduler.${this.clusterName}.${this.awsRegion}.local`,
        PBS_HOME: SCHEDULER_PBS_HOME,
        PBS_NODE_FAIL_REQUEUE: '600',
      },
      healthCheck: {
        command: [
          'CMD-SHELL',
          'qstat -B && curl --fail --silent --show-error --unix-socket /run/idea.sock --max-time 4 --header \'Content-Type: application/json\' --data \'{"header":{"namespace":"Scheduler.ListActiveJobs"}}\' http://localhost/scheduler/api/v1',
        ],
        interval: Duration.seconds(30),
        retries: 3,
        // The container check has no allowance but this one, on a first start and on every
        // replacement, so it matches the load balancer grace. A shorter period would let the
        // platform kill the container while `roles/scheduler.sh` is still inside its own wait for
        // the batch server.
        startPeriod: taskStartAllowance('scheduler'),
      },
      image: containerImage(scope),
      logging: adoptedLogDriver(scope, 'scheduler-log-group', logGroupName, STREAM_PREFIX_APPLICATION),
      memoryLimitMiB: sizing.memory,
    });
    container.addPortMappings({ containerPort: 8443, protocol: ecs.Protocol.TCP });
    this.addSchedulerStorage(taskDefinition, container, taskRole);
    addStorageMounts(scope, taskDefinition, container);
    attachApplicationFileLogs(scope, { idPrefix: 'scheduler', logGroupName, taskDefinition, container });
    addLogTailContainer(scope, taskDefinition, {
      containerId: 'scheduler-openpbs-logs',
      directories: [
        `${SCHEDULER_PBS_HOME}/server_logs`,
        `${SCHEDULER_PBS_HOME}/sched_logs`,
        `${SCHEDULER_PBS_HOME}/server_priv/accounting`,
      ],
      logGroupName: `${logGroupName}/openpbs`,
      logGroupConstructId: 'scheduler-openpbs-log-group',
      streamPrefix: STREAM_PREFIX_OPENPBS,
      sourceVolume: 'scheduler-pbs',
      containerPath: SCHEDULER_PBS_HOME,
      readOnly: true,
    });

    const service = buildEc2Service(scope, {
      constructId: 'scheduler-service',
      serviceName: this.containerServiceName,
      taskDefinition,
      desiredCount: sizing.desired,
      securityGroups: [this.schedulerSecurityGroup],
      minHealthyPercent: 0,
      maxHealthyPercent: 100,
      distinctInstances: false,
      healthCheckGracePeriod: healthCheckGrace('scheduler'),
      dependencies: [taskRole, taskPolicy, executionRole, this.externalEndpoint, this.internalEndpoint],
    });
    for (const targetGroup of this.containerTargetGroups) service.attachToApplicationTargetGroup(targetGroup);
    this.containerService = service;
  }

  /** Adds the scheduler-only EFS file system that keeps PBS state across a task replacement. */
  private addSchedulerStorage(
    taskDefinition: ecs.Ec2TaskDefinition,
    container: ecs.ContainerDefinition,
    taskRole: iam.IRole,
  ): void {
    const fileSystemSecurityGroup = new ec2.SecurityGroup(
      this.stack,
      'scheduler-pbs-file-system-security-group',
      {
        allowAllOutbound: false,
        description: 'Allows NFS only from the scheduler task and its host',
        vpc: this.cluster.vpc,
      },
    );
    fileSystemSecurityGroup.addIngressRule(
      this.schedulerSecurityGroup,
      ec2.Port.tcp(2049),
      'Allow NFS from the scheduler task',
    );
    // Which interface carries the mount, the task's or the container host's, is a property of the
    // container agent rather than of this template, and the mount fails silently from the wrong one.
    // The host group belongs to the container stack, which publishes its id, and the file system
    // policy below is what actually limits access: only the scheduler task role, and only through
    // its access point.
    fileSystemSecurityGroup.addIngressRule(
      ec2.SecurityGroup.fromSecurityGroupId(
        this.stack,
        'ecs-host-security-group',
        this.context.config.getString('ecs.host_security_group_id', undefined, {
          required: true,
        }) as string,
      ),
      ec2.Port.tcp(2049),
      'Allow NFS from the container host that mounts for the scheduler task',
    );

    const fileSystem = new efs.FileSystem(this.stack, 'scheduler-pbs-file-system', {
      encrypted: true,
      securityGroup: fileSystemSecurityGroup,
      vpc: this.cluster.vpc,
      vpcSubnets: { subnets: this.cluster.privateSubnets },
    });
    fileSystem.applyRemovalPolicy(RemovalPolicy.RETAIN);
    const accessPoint = fileSystem.addAccessPoint('scheduler-pbs-access-point', {
      createAcl: { ownerGid: '0', ownerUid: '0', permissions: '0700' },
      path: '/pbs',
      posixUser: { gid: '0', uid: '0' },
    });
    accessPoint.applyRemovalPolicy(RemovalPolicy.RETAIN);

    const clientActions = [
      'elasticfilesystem:ClientMount',
      'elasticfilesystem:ClientWrite',
      'elasticfilesystem:ClientRootAccess',
    ];
    // The policy is a property of the file system, so naming the file system in it resolves an
    // attribute of the resource the policy belongs to. CloudFormation counts that self reference as
    // a circular dependency and refuses the template. A file system policy applies only to the file
    // system carrying it, so the resource element does not have to name it. The same rule rules out
    // naming the access point, so the condition below requires the shape of one instead, which is
    // the same restriction while this file system has the single access point created above.
    const OWN_FILE_SYSTEM = '*';
    const accessPointOfThisFileSystem =
      `arn:${Aws.PARTITION}:elasticfilesystem:${Aws.REGION}:${Aws.ACCOUNT_ID}:access-point/*`;
    fileSystem.addToResourcePolicy(
      new iam.PolicyStatement({
        actions: clientActions,
        conditions: { Bool: { 'elasticfilesystem:AccessedViaMountTarget': 'true' } },
        principals: [new iam.ArnPrincipal(taskRole.roleArn)],
        resources: [OWN_FILE_SYSTEM],
      }),
    );
    fileSystem.addToResourcePolicy(
      new iam.PolicyStatement({
        actions: clientActions,
        conditions: { ArnNotEquals: { 'aws:PrincipalArn': taskRole.roleArn } },
        effect: iam.Effect.DENY,
        principals: [new iam.AnyPrincipal()],
        resources: [OWN_FILE_SYSTEM],
      }),
    );
    fileSystem.addToResourcePolicy(
      new iam.PolicyStatement({
        actions: clientActions,
        // A negated string condition is true when the key is absent, so a mount that presents no
        // access point at all is denied by this statement as well.
        conditions: {
          StringNotLike: { 'elasticfilesystem:AccessPointArn': accessPointOfThisFileSystem },
        },
        effect: iam.Effect.DENY,
        principals: [new iam.AnyPrincipal()],
        resources: [OWN_FILE_SYSTEM],
      }),
    );

    taskDefinition.addVolume({
      efsVolumeConfiguration: {
        authorizationConfig: { accessPointId: accessPoint.accessPointId, iam: 'ENABLED' },
        fileSystemId: fileSystem.fileSystemId,
        rootDirectory: '/',
        transitEncryption: 'ENABLED',
      },
      name: 'scheduler-pbs',
    });
    container.addMountPoints({
      containerPath: SCHEDULER_PBS_HOME,
      readOnly: false,
      sourceVolume: 'scheduler-pbs',
    });
  }

  buildEc2Instance(): void {
    const config = this.context.config;
    const isPublic = config.getBool('scheduler.public', false);
    const baseOs = config.getString('scheduler.base_os', undefined, { required: true }) as string;
    const instanceAmi = config.getString('scheduler.instance_ami', undefined, { required: true }) as string;
    const instanceType = config.getString('scheduler.instance_type', undefined, { required: true }) as string;
    const volumeSize = config.getInt('scheduler.volume_size', 200);
    const keyPair = lookupKeyPair(this.context, this.stack);
    const enableDetailedMonitoring = config.getBool('scheduler.ec2.enable_detailed_monitoring', false);
    const enableTerminationProtection = config.getBool('scheduler.ec2.enable_termination_protection', false);
    const metadataHttpTokens = config.getString('scheduler.ec2.metadata_http_tokens', undefined, {
      required: true,
    }) as string;
    const httpsProxy = config.getString('cluster.network.https_proxy', '');
    const noProxy = config.getString('cluster.network.no_proxy', '');
    const proxyConfig: Record<string, string> =
      httpsProxy === '' ? {} : { http_proxy: httpsProxy, https_proxy: httpsProxy, no_proxy: noProxy };
    const ebsKmsKey = lookupEbsKmsKey(this.context, this.stack);

    const usePublicSubnets = isPublic && this.cluster.publicSubnets.length > 0;
    const subnetIds = usePublicSubnets
      ? this.cluster.existingVpc.getPublicSubnetIds()
      : this.cluster.existingVpc.getPrivateSubnetIds();
    // Python indexes the list and raises IndexError on an empty one. Without this the template
    // synthesizes with no SubnetId and the deploy fails halfway, after the roles and the queues.
    const subnetId = subnetIds[0];
    if (subnetId === undefined) {
      throw new Error(
        `cluster.network.${usePublicSubnets ? 'public' : 'private'}_subnets is empty: no subnet to launch the scheduler into`,
      );
    }

    const blockDeviceName = ec2BlockDeviceName(baseOs);
    const blockDeviceTypeString = config.getString('scheduler.volume_type', 'gp3');
    const blockDeviceVolumeType =
      blockDeviceTypeString === 'gp3' ? ec2.EbsDeviceVolumeType.GP3 : ec2.EbsDeviceVolumeType.GP2;

    const userData = buildBootstrapUserData({
      awsRegion: this.awsRegion,
      bootstrapPackageUri: this.bootstrapPackageUri,
      installCommands: ['/bin/bash scheduler/setup.sh'],
      proxyConfig,
      baseOs,
    });

    const launchTemplate = new ec2.LaunchTemplate(this.stack, `${this.moduleId}-lt`, {
      instanceType: new ec2.InstanceType(instanceType),
      machineImage: ec2.MachineImage.genericLinux({ [this.awsRegion]: instanceAmi }),
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
      requireImdsv2: metadataHttpTokens === 'required',
    });

    this.ec2Instance = new ec2.CfnInstance(this.stack, `${this.moduleId}-instance`, {
      blockDeviceMappings: [
        {
          deviceName: blockDeviceName,
          ebs: { volumeSize, volumeType: blockDeviceTypeString },
        },
      ],
      disableApiTermination: enableTerminationProtection,
      // The profile name, not a Ref: there is no dependency edge to the instance profile.
      iamInstanceProfile: this.schedulerInstanceProfile.instanceProfileName as string,
      instanceType,
      imageId: instanceAmi,
      keyName: keyPair.keyPairName,
      launchTemplate: {
        version: launchTemplate.latestVersionNumber,
        launchTemplateId: launchTemplate.launchTemplateId as string,
      },
      networkInterfaces: [
        {
          deviceIndex: '0',
          associatePublicIpAddress: isPublic,
          groupSet: [this.schedulerSecurityGroup.securityGroupId],
          subnetId,
        },
      ],
      userData: Fn.base64(Fn.sub(userData)),
      monitoring: enableDetailedMonitoring,
    });
    Tags.of(this.ec2Instance).add('Name', this.buildResourceName(this.moduleId));
    Tags.of(this.ec2Instance).add(IDEA_TAG_NODE_TYPE, NODE_TYPE_APP);
    this.addBackupTags(this.ec2Instance);

    this.addNagSuppression(
      [{ rule_id: 'AwsSolutions-EC26', reason: 'EBS Encryption is enforced via Launch Template' }],
      this.ec2Instance,
    );

    if (!enableDetailedMonitoring) {
      this.addNagSuppression(
        [
          {
            rule_id: 'AwsSolutions-EC28',
            reason: 'detailed monitoring is a configurable option to save costs',
          },
        ],
        this.ec2Instance,
      );
    }

    if (!enableTerminationProtection) {
      this.addNagSuppression(
        [
          {
            rule_id: 'AwsSolutions-EC29',
            reason:
              'termination protection not supported in CDK L2 construct. enable termination protection via AWS EC2 console after deploying the cluster.',
          },
        ],
        this.ec2Instance,
      );
    }
  }

  buildRoute53RecordSet(): void {
    const hostname = this.context.config.getString('scheduler.hostname', undefined, {
      required: true,
    }) as string;
    this.clusterDnsRecordSet = new route53.RecordSet(this.stack, `${this.moduleId}-dns-record`, {
      recordType: route53.RecordType.A,
      target: route53.RecordTarget.fromIpAddresses(this.ec2Instance.attrPrivateIp),
      ttl: Duration.minutes(5),
      recordName: hostname,
      zone: lookupClusterDns(this.context, this.stack),
    });
    // The record is the name clients and execution hosts resolve. Retaining it lets a later
    // deploy stop managing it, which is what the container path does, without CloudFormation
    // deleting the name. Deploy this on its own before turning `ecs.enabled` on.
    if (this.context.config.getBool('scheduler.retain_dns_record', false)) {
      this.clusterDnsRecordSet.applyRemovalPolicy(RemovalPolicy.RETAIN);
    }
  }

  buildEndpoints(): void {
    const config = this.context.config;
    const externalTargetGroupArn = this.ecsEnabled
      ? this.containerTargetGroupArn(0)
      : new elbv2.CfnTargetGroup(
          this.stack,
          `${this.moduleId}-external-target-group`,
          {
            port: 8443,
            protocol: 'HTTPS',
            targetType: 'ip',
            vpcId: this.cluster.vpc.vpcId,
            name: this.getTargetGroupName('sched-ext'),
            targets: [{ id: this.ec2Instance.attrPrivateIp }],
            healthCheckPath: '/healthcheck',
          },
        ).ref;
    const clusterEndpointsLambdaArn = config.getString('cluster.cluster_endpoints_lambda_arn', undefined, {
      required: true,
    }) as string;
    const externalHttpsListenerArn = config.getString(
      'cluster.load_balancers.external_alb.https_listener_arn',
      undefined,
      { required: true },
    ) as string;
    const externalEndpointPriority = requiredInt(config, 'scheduler.endpoints.external.priority');
    const externalEndpointPathPatterns = requiredList(config, 'scheduler.endpoints.external.path_patterns');

    this.externalEndpoint = new CustomResource(this.stack, 'external-endpoint', {
      serviceToken: clusterEndpointsLambdaArn,
      properties: {
        endpoint_name: `${this.moduleId}-external-endpoint`,
        listener_arn: externalHttpsListenerArn,
        priority: externalEndpointPriority,
        target_group_arn: externalTargetGroupArn,
        conditions: [{ Field: 'path-pattern', Values: externalEndpointPathPatterns }],
        actions: [{ Type: 'forward', TargetGroupArn: externalTargetGroupArn }],
        tags: {
          [IDEA_TAG_CLUSTER_NAME]: this.clusterName,
          [IDEA_TAG_MODULE_ID]: this.moduleId,
          [IDEA_TAG_MODULE_NAME]: MODULE_SCHEDULER,
        },
      },
      resourceType: 'Custom::SchedulerEndpointExternal',
    });

    const internalHttpsListenerArn = config.getString(
      'cluster.load_balancers.internal_alb.https_listener_arn',
      undefined,
      { required: true },
    ) as string;
    const internalEndpointPriority = requiredInt(config, 'scheduler.endpoints.internal.priority');
    const internalEndpointPathPatterns = requiredList(config, 'scheduler.endpoints.internal.path_patterns');

    const internalTargetGroupArn = this.ecsEnabled
      ? this.containerTargetGroupArn(1)
      : new elbv2.CfnTargetGroup(
          this.stack,
          `${this.moduleId}-internal-target-group`,
          {
            port: 8443,
            protocol: 'HTTPS',
            targetType: 'ip',
            vpcId: this.cluster.vpc.vpcId,
            name: this.getTargetGroupName('sched-int'),
            targets: [{ id: this.ec2Instance.attrPrivateIp }],
            healthCheckPath: '/healthcheck',
          },
        ).ref;

    this.internalEndpoint = new CustomResource(this.stack, 'internal-endpoint', {
      serviceToken: clusterEndpointsLambdaArn,
      properties: {
        endpoint_name: `${this.moduleId}-internal-endpoint`,
        listener_arn: internalHttpsListenerArn,
        priority: internalEndpointPriority,
        target_group_arn: internalTargetGroupArn,
        conditions: [{ Field: 'path-pattern', Values: internalEndpointPathPatterns }],
        actions: [{ Type: 'forward', TargetGroupArn: internalTargetGroupArn }],
        tags: {
          [IDEA_TAG_CLUSTER_NAME]: this.clusterName,
          [IDEA_TAG_MODULE_ID]: this.moduleId,
          [IDEA_TAG_MODULE_NAME]: MODULE_SCHEDULER,
        },
      },
      resourceType: 'Custom::SchedulerEndpointInternal',
    });
  }

  buildClusterSettings(): void {
    // The scheduler and the execution hosts both derive the PBS server name from
    // private_dns_name. Pointing it at the cluster DNS record rather than the instance lets a
    // replaced scheduler keep its name, so execution hosts do not need reconfiguring and running
    // jobs survive. Off by default: turning it on for an existing cluster renames its PBS server,
    // and execution hosts already running jobs would not follow the change.
    const useStableServerName =
      this.ecsEnabled || this.context.config.getBool('scheduler.use_stable_server_name', false);
    const privateDnsName = useStableServerName
      ? (this.context.config.getString('scheduler.hostname', undefined, { required: true }) as string)
      : this.ec2Instance.attrPrivateDnsName;

    const clusterSettings: Record<string, unknown> = {
      deployment_id: this.deploymentId,
      private_dns_name: privateDnsName,
    };

    if (this.ecsEnabled) {
      // The container path has no instance address to pin the name with, so the row that makes the
      // scheduler and the execution hosts use the DNS name is written, not assumed.
      clusterSettings['use_stable_server_name'] = true;
    } else {
      clusterSettings['private_ip'] = this.ec2Instance.attrPrivateIp;
      if (this.context.config.getBool('scheduler.public', false)) {
        clusterSettings['public_ip'] = this.ec2Instance.attrPublicIp;
      }
      clusterSettings['instance_id'] = this.ec2Instance.ref;
    }

    clusterSettings['client_id'] = this.oauth2ClientSecret.clientId.ref;
    clusterSettings['client_secret'] = this.oauth2ClientSecret.clientSecret.ref;
    clusterSettings['security_group_id'] = this.schedulerSecurityGroup.securityGroupId;
    clusterSettings['iam_role_arn'] = this.schedulerRole.roleArn;
    clusterSettings['compute_node_security_group_ids'] = [this.computeNodeSecurityGroup.securityGroupId];
    clusterSettings['compute_node_iam_role_arn'] = this.computeNodeRole.roleArn;
    clusterSettings['compute_node_instance_profile_arn'] = this.computeNodeInstanceProfile.ref;
    clusterSettings['spot_fleet_request_iam_role_arn'] = this.spotFleetRequestRole.roleArn;
    clusterSettings['job_status_sqs_queue_url'] = this.jobStatusSqsQueue.queueUrl;

    // iam:PassRole for project roles is granted only at deploy time when bedrock-for-jobs is
    // enabled, not by toggling it at runtime; written under the same gate so the scheduler can
    // detect a refusal.
    if (this.isBedrockEnabledForJobs()) {
      clusterSettings['bedrock.project_pass_role_arn'] = this.arnBuilder.getProjectRoleArn();
    }

    const settings = this.updateClusterSettings(clusterSettings);
    // The task reads `client_id`, `private_dns_name` and the rest of this module's rows at boot,
    // so it must not start before they are written.
    this.containerService?.node.addDependency(settings);
  }
}

export function buildStack(props: StackBuildProps): void {
  new SchedulerStack(props);
}
