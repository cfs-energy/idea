/**
 * Everything the stack references from another module is a literal read out of the cluster
 * config at synth time: there are no exports, no `Fn::ImportValue` and no outputs. The three
 * IAM policies take CDK's default `PolicyName`, which is the logical id, so the construct ids
 * here are load bearing twice over.
 */

import { CustomResource, Duration, Fn, RemovalPolicy, Tags } from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
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
    this.buildEndpoints();
    this.buildClusterSettings();
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

    const oauthCredentialsLambdaArn = this.context.config.getString(
      'identity-provider.cognito.oauth_credentials_lambda_arn',
      undefined,
      { required: true },
    ) as string;
    const clientSecret = new CustomResource(this.stack, `${this.moduleId}-creds`, {
      serviceToken: oauthCredentialsLambdaArn,
      properties: {
        UserPoolId: this.userPool.userPoolId,
        ClientId: client.userPoolClientId,
      },
      resourceType: 'Custom::GetOAuthCredentials',
    });

    this.oauth2ClientSecret = new OAuthClientIdAndSecret(
      this.context,
      this.moduleId,
      MODULE_SCHEDULER,
      this.stack,
      client.userPoolClientId,
      clientSecret.getAttString('ClientSecret'),
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

  private ecsTargetGroupArn(index: number): string {
    const targetGroupArns = this.context.config.getList<string>(
      "ecs.scheduler.target_group_arns",
      [],
      { required: true },
    );
    const targetGroupArn = targetGroupArns[index];
    if (targetGroupArn === undefined || targetGroupArn === "") {
      throw new Error(`ecs.scheduler.target_group_arns[${index}] is required when ecs.enabled is true`);
    }
    return targetGroupArn;
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
      ? this.ecsTargetGroupArn(0)
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
      ? this.ecsTargetGroupArn(1)
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

    this.updateClusterSettings(clusterSettings);
  }
}

export function buildStack(props: StackBuildProps): void {
  new SchedulerStack(props);
}
