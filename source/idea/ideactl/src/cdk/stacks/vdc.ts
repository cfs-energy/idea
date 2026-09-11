/**
 * Three EC2 host groups (controller, DCV broker, DCV connection gateway), the external network
 * load balancer the gateway sits behind, the DCV host (VDI) identity used by session instances,
 * the SQS/SNS plumbing the controller listens on, and the scheduled-event transformer Lambda.
 *
 * Build order is load-bearing: it fixes the statement order in
 * the controller role's CDK-generated `DefaultPolicy` and the order of the security-group rules.
 *
 * Load-bearing behaviour:
 *
 * - the DCV broker *agent* endpoint registers itself as `broker-client-endpoint`; the name is the
 *   custom resource's physical identity, so correcting it would re-register the ALB rule;
 * - `instanceMonitoring` is passed alongside a launch template. `Monitoring.BASIC` is 0, so the
 *   CDK guard against launch-configuration properties does not fire; detailed monitoring would
 *   throw at synth. The config key the settings template writes never matches the one read here,
 *   so the value is always `BASIC`;
 * - `cluster.backups.enabled` is read with `getString`, so a boolean `false` arrives as the
 *   non-empty string `false` and passes the guard. The second guard on
 *   `vdi_host_backup.enabled` is what actually decides;
 * - target groups are attached by overwriting `targetGroupArns` on the L1. The array order is
 *   part of the deployed template.
 */

import { CustomResource, Duration, Fn, Tags } from 'aws-cdk-lib';
import * as autoscaling from 'aws-cdk-lib/aws-autoscaling';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as events from 'aws-cdk-lib/aws-events';
import * as eventsTargets from 'aws-cdk-lib/aws-events-targets';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as snsSubscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as sqs from 'aws-cdk-lib/aws-sqs';

import { ArnBuilder } from '../../config/arn-builder.ts';
import type { ClusterConfig } from '../../config/cluster-config.ts';
import type { StackBuildProps } from '../app.ts';
import { IdeaBaseStack } from '../base-stack.ts';
import { IdeaCodeAsset } from '../code-asset.ts';
import { buildBootstrapUserData } from '../userdata.ts';
import {
  IDEA_TAG_NAME,
  IDEA_TAG_NODE_TYPE,
  instanceProfileArn,
} from '../constructs/base.ts';
import { BackupPlan } from '../constructs/backup.ts';
import {
  InstanceProfile,
  LambdaFunction,
  ManagedPolicy,
  Policy,
  Role,
  SNSTopic,
  SQSQueue,
} from '../constructs/common.ts';
import { OAuthClientIdAndSecret } from '../constructs/directory-service.ts';
import {
  ExistingSocaCluster,
  lookupBackupRole,
  lookupClusterBackupVault,
  lookupClusterS3Bucket,
  lookupEbsKmsKey,
  lookupEc2StateChangeTopic,
  lookupKeyPair,
} from '../constructs/existing-resources.ts';
import {
  VirtualDesktopBastionAccessSecurityGroup,
  VirtualDesktopBrokerSecurityGroup,
  VirtualDesktopPublicLoadBalancerAccessSecurityGroup,
  type SecurityGroup,
} from '../constructs/network.ts';

/** `constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER`. */
const MODULE_VIRTUAL_DESKTOP_CONTROLLER = 'virtual-desktop-controller';
/** `constants.MODULE_CLUSTER_MANAGER`. */
const MODULE_CLUSTER_MANAGER = 'cluster-manager';
/** `constants.IDEA_TAG_MODULE_ID`, with `:` replaced by `_` for the SNS filter policy key. */
const IDEA_TAG_MODULE_ID = 'idea:ModuleId';
/** `constants.NODE_TYPE_APP` / `NODE_TYPE_INFRA`. */
const NODE_TYPE_APP = 'app';
const NODE_TYPE_INFRA = 'infra';
/** `constants.SQS_MAX_RECEIVE_COUNT_DEFAULT` and `SQS_VISIBILITY_TIMEOUT_DEFAULT`. */
const SQS_MAX_RECEIVE_COUNT_DEFAULT = 16;
const SQS_VISIBILITY_TIMEOUT_DEFAULT = 30;
/** GovCloud does not accept `Tags` on `AWS::Events::Rule`. */
const AWS_PARTITION_GOVCLOUD = 'aws-us-gov';
const OS_AMAZONLINUX2 = 'amazonlinux2';
const OS_AMAZONLINUX2023 = 'amazonlinux2023';

const COMPONENT_CONTROLLER = 'controller';
const COMPONENT_DCV_BROKER = 'broker';
const COMPONENT_DCV_CONNECTION_GATEWAY = 'gateway';
const COMPONENT_DCV_HOST = 'host';

/** Component id -> the name the config keys use. */
const CONFIG_MAPPING: Record<string, string> = {
  [COMPONENT_CONTROLLER]: 'controller',
  [COMPONENT_DCV_CONNECTION_GATEWAY]: 'dcv_connection_gateway',
  [COMPONENT_DCV_BROKER]: 'dcv_broker',
};

/**
 * `get_bool(key, required=True)`, `get_int(...)` and `get_list(...)`. Unlike `getString`, these
 * getters have no overload for an undefined default, so the call is typed here instead.
 */
type RequiredGet<T> = (key: string, defaultValue?: T, options?: { required?: boolean }) => T;

function requiredBool(config: ClusterConfig, key: string): boolean {
  return (config.getBool as RequiredGet<boolean>)(key, undefined, { required: true });
}

function requiredInt(config: ClusterConfig, key: string): number {
  return (config.getInt as RequiredGet<number>)(key, undefined, { required: true });
}

function requiredList(config: ClusterConfig, key: string): string[] {
  return (config.getList as RequiredGet<string[]>)(key, undefined, { required: true });
}

/** `Utils.get_ec2_block_device_name`. */
function ec2BlockDeviceName(baseOs: string): string {
  return baseOs === OS_AMAZONLINUX2 || baseOs === OS_AMAZONLINUX2023 ? '/dev/xvda' : '/dev/sda1';
}

interface AutoScalingGroupOptions {
  componentName: string;
  securityGroup: SecurityGroup;
  iamRole: Role;
  substitutedUserdata: string;
  nodeType: string;
}

export class VirtualDesktopControllerStack extends IdeaBaseStack {
  readonly cluster: ExistingSocaCluster;
  readonly arnBuilder: ArnBuilder;
  readonly userPool: cognito.IUserPool;

  readonly brokerClientCommunicationPort: number;
  readonly brokerAgentCommunicationPort: number;
  readonly brokerGatewayCommunicationPort: number;
  readonly clusterEndpointsLambdaArn: string;
  private readonly ecsEnabled: boolean;
  private readonly hostsPresent: boolean;

  oauth2ClientSecret!: OAuthClientIdAndSecret;

  dcvHostRole!: Role;
  controllerRole!: Role;
  dcvBrokerRole!: Role;
  dcvConnectionGatewayRole!: Role;
  scheduledEventTransformerLambdaRole!: Role;
  dcvHostInstanceProfile!: InstanceProfile;
  dcvHostPolicy!: ManagedPolicy;

  dcvHostSecurityGroup!: VirtualDesktopBastionAccessSecurityGroup;
  controllerSecurityGroup!: VirtualDesktopPublicLoadBalancerAccessSecurityGroup;
  dcvConnectionGatewaySecurityGroup!: VirtualDesktopPublicLoadBalancerAccessSecurityGroup;
  dcvBrokerSecurityGroup!: VirtualDesktopBrokerSecurityGroup;

  dcvConnectionGatewaySelfSignedCert: CustomResource | undefined;
  externalNlb!: elbv2.NetworkLoadBalancer;

  eventSqsQueue!: SQSQueue;
  eventSqsQueueDlq!: SQSQueue;
  controllerSqsQueue!: SQSQueue;
  controllerSqsQueueDlq!: SQSQueue;
  ssmCommandsSnsTopic!: SNSTopic;
  ssmCommandPassRole!: Role;

  controllerAutoScalingGroup!: autoscaling.AutoScalingGroup;
  dcvBrokerAutoScalingGroup!: autoscaling.AutoScalingGroup;
  dcvConnectionGatewayAutoScalingGroup!: autoscaling.AutoScalingGroup;

  backupPlan: BackupPlan | undefined;

  constructor(props: StackBuildProps) {
    super({
      scope: props.app,
      ctx: props.ctx,
      moduleName: props.moduleName,
      deploymentId: props.deploymentId,
      terminationProtection: props.terminationProtection,
      env: props.env,
    });

    const config = this.context.config;
    this.ecsEnabled = config.getBool("ecs.enabled", false);
    // `ecs.enabled` alone routes the endpoints to the container services and deletes the three host
    // groups that serve them in one change set, so nothing proves the new targets before the old
    // ones are gone. With `ecs.retain_existing_hosts` the same deploy routes the endpoints and keeps
    // the groups, idle and unregistered, so a later deploy removes them once the containers are
    // serving and turning the flag off puts the groups back in service.
    this.hostsPresent = !this.ecsEnabled || config.getBool("ecs.retain_existing_hosts", false);
    this.brokerClientCommunicationPort = requiredInt(
      config,
      'virtual-desktop-controller.dcv_broker.client_communication_port',
    );
    this.brokerAgentCommunicationPort = requiredInt(
      config,
      'virtual-desktop-controller.dcv_broker.agent_communication_port',
    );
    this.brokerGatewayCommunicationPort = requiredInt(
      config,
      'virtual-desktop-controller.dcv_broker.gateway_communication_port',
    );
    this.clusterEndpointsLambdaArn = config.getString('cluster.cluster_endpoints_lambda_arn', undefined, {
      required: true,
    }) as string;

    this.cluster = new ExistingSocaCluster(this.context, this.stack);
    this.arnBuilder = new ArnBuilder(config);

    this.userPool = this.lookupUserPool();

    this.buildOauth2Client();
    this.buildAccessControlGroups(this.userPool);

    this.buildSqsQueues();
    this.buildScheduledEventNotificationInfra();
    this.subscribeToEc2NotificationEvents();

    this.buildVirtualDesktopController();
    this.buildDcvBroker();
    this.buildDcvConnectionGateway();
    this.buildDcvHostInfra();

    this.buildControllerSsmCommandsNotificationInfra();
    this.buildBackups();

    this.setupEgressRulesForQuic();
    this.buildClusterSettings();
  }

  // --- helpers ----------------------------------------------------------------------------------

  private requiredString(key: string): string {
    return this.context.config.getString(key, undefined, { required: true }) as string;
  }

  /** A cluster security group by name; `undefined` here is a cluster-config error. */
  private clusterSecurityGroup(name: string): ec2.ISecurityGroup {
    const securityGroup = this.cluster.getSecurityGroup(name);
    if (securityGroup === undefined) {
      throw new Error(`cluster.network.security_groups.${name} not found`);
    }
    return securityGroup;
  }

  /** The `http_proxy`/`https_proxy`/`no_proxy` block every host group shares. */
  private proxyConfig(): Record<string, string> {
    const httpsProxy = this.context.config.getString('cluster.network.https_proxy', '');
    if (httpsProxy === '') return {};
    return {
      http_proxy: httpsProxy,
      https_proxy: httpsProxy,
      no_proxy: this.context.config.getString('cluster.network.no_proxy', ''),
    };
  }

  /** A `<component>` config key under the module namespace. */
  private componentKey(componentName: string, suffix: string): string {
    return `virtual-desktop-controller.${CONFIG_MAPPING[componentName] as string}.${suffix}`;
  }


  /**
   * Bootstrap package location for one desktop component.
   *
   * The deploy path uploads the package and passes its location as context. A standalone synthesis
   * has no context, and this used to fall back to the literal string for absent, which rendered
   * straight into host user data: the host then tried to download a package called "not-provided".
   * It also made every comparison against a real cluster's deployed template report three
   * differences that were the harness's fault rather than the port's. The sibling stacks derive the
   * location from the deployment identifier instead, by the same naming rule the uploader uses, so
   * this does too.
   */
  private componentBootstrapPackageUri(contextKey: string, componentSuffix: string): string {
    const fromContext: unknown = this.stack.node.tryGetContext(contextKey);
    if (typeof fromContext === 'string' && fromContext !== '') return fromContext;
    const bucket = this.context.config.getString('cluster.cluster_s3_bucket', undefined, {
      required: true,
    }) as string;
    return (
      `s3://${bucket}/idea/bootstrap/bootstrap-${this.moduleId}-${componentSuffix}`
      + `-${this.deploymentId}.tar.gz`
    );
  }

  private removeBastionHostIngressRule(securityGroup: SecurityGroup): void {
    const ingressRule = securityGroup.node.children.find(
      (child) =>
        child instanceof ec2.CfnSecurityGroupIngress &&
        child.description === "Allow SSH from Bastion Host",
    );
    if (ingressRule === undefined) {
      throw new Error("VDC security group has no bastion SSH ingress rule");
    }
    securityGroup.node.tryRemoveChild(ingressRule.node.id);
  }

  private ecsTargetGroupArn(role: string, index: number): string {
    const targetGroupArns = this.context.config.getList<string>(
      `ecs.${role}.target_group_arns`,
      [],
      { required: true },
    );
    const targetGroupArn = targetGroupArns[index];
    if (targetGroupArn === undefined || targetGroupArn === "") {
      throw new Error(`ecs.${role}.target_group_arns[${index}] is required when ecs.enabled is true`);
    }
    return targetGroupArn;
  }

  private ecsServiceName(role: string): string {
    const serviceArn = this.requiredString(`ecs.${role}.service_arn`);
    const serviceName = serviceArn.split("/").at(-1);
    if (serviceName === undefined || serviceName === "") {
      throw new Error(`ecs.${role}.service_arn does not contain a service name`);
    }
    return serviceName;
  }

  // --- QUIC -------------------------------------------------------------------------------------

  setupEgressRulesForQuic(): void {
    const quicSupported = requiredBool(
      this.context.config,
      'virtual-desktop-controller.dcv_session.quic_support',
    );
    if (!quicSupported) return;

    this.dcvHostSecurityGroup.addEgressRule(
      ec2.Peer.ipv4('0.0.0.0/0'),
      ec2.Port.udpRange(0, 65535),
      'Allow all egress for UDP for QUIC Support on DCV Host',
    );
    this.dcvHostSecurityGroup.addEgressRule(
      ec2.Peer.ipv6('::/0'),
      ec2.Port.udpRange(0, 65535),
      'Allow all egress for UDP for QUIC Support on DCV Host',
    );

    this.dcvConnectionGatewaySecurityGroup.addEgressRule(
      ec2.Peer.ipv4('0.0.0.0/0'),
      ec2.Port.udpRange(0, 65535),
      'Allow all egress for UDP for QUIC Support on DCV Connection Gateway',
    );
    this.dcvConnectionGatewaySecurityGroup.addEgressRule(
      ec2.Peer.ipv6('::/0'),
      ec2.Port.udpRange(0, 65535),
      'Allow all egress for UDP for QUIC Support on DCV Connection Gateway',
    );
  }

  // --- messaging --------------------------------------------------------------------------------

  buildSqsQueues(): void {
    const config = this.context.config;

    this.eventSqsQueueDlq = new SQSQueue(
      this.context,
      'virtual-desktop-controller-events-queue-dlq',
      this.stack,
      {
        queueName: `${this.clusterName}-${this.moduleId}-events-dlq.fifo`,
        fifo: true,
        fifoThroughputLimit: sqs.FifoThroughputLimit.PER_MESSAGE_GROUP_ID,
        deduplicationScope: sqs.DeduplicationScope.MESSAGE_GROUP,
        contentBasedDeduplication: true,
        encryptionMasterKey: config.getString('cluster.sqs.kms_key_id'),
        isDeadLetterQueue: true,
      },
    );
    this.eventSqsQueue = new SQSQueue(this.context, 'virtual-desktop-controller-events-queue', this.stack, {
      queueName: `${this.clusterName}-${this.moduleId}-events.fifo`,
      fifo: true,
      fifoThroughputLimit: sqs.FifoThroughputLimit.PER_MESSAGE_GROUP_ID,
      deduplicationScope: sqs.DeduplicationScope.MESSAGE_GROUP,
      contentBasedDeduplication: true,
      encryptionMasterKey: config.getString('cluster.sqs.kms_key_id'),
      deadLetterQueue: { maxReceiveCount: SQS_MAX_RECEIVE_COUNT_DEFAULT, queue: this.eventSqsQueueDlq },
    });
    this.addCommonTags(this.eventSqsQueue);
    this.addCommonTags(this.eventSqsQueueDlq);

    const kmsKey = config.getString('cluster.sqs.kms_key_id');
    const encryptAtRest = kmsKey !== undefined && kmsKey !== '';

    this.controllerSqsQueueDlq = new SQSQueue(
      this.context,
      'virtual-desktop-controller-queue-dlq',
      this.stack,
      {
        queueName: `${this.clusterName}-${this.moduleId}-controller-dlq`,
        encryptAtRest,
        encryptionMasterKey: kmsKey,
        isDeadLetterQueue: true,
      },
    );
    this.controllerSqsQueue = new SQSQueue(this.context, 'virtual-desktop-controller-queue', this.stack, {
      queueName: `${this.clusterName}-${this.moduleId}-controller`,
      encryptAtRest,
      encryptionMasterKey: kmsKey,
      visibilityTimeout: Duration.seconds(SQS_VISIBILITY_TIMEOUT_DEFAULT),
      deadLetterQueue: {
        maxReceiveCount: SQS_MAX_RECEIVE_COUNT_DEFAULT,
        queue: this.controllerSqsQueueDlq,
      },
    });
    this.addCommonTags(this.controllerSqsQueue);
    this.addCommonTags(this.controllerSqsQueueDlq);
  }

  buildControllerSsmCommandsNotificationInfra(): void {
    this.ssmCommandPassRole = new Role(
      this.context,
      `${this.moduleId}-ssm-commands-sns-topic-role`,
      this.stack,
      {
        assumedBy: ['ssm'],
        description: 'IAM role for SSM Commands to send notifications via SNS',
      },
    );

    this.ssmCommandPassRole.attachInlinePolicy(
      new Policy(
        this.context,
        `${this.clusterName}-${this.moduleId}-ssm-commands-sns-topic-role-policy`,
        this.stack,
        { policyTemplateName: 'controller-ssm-command-pass-role.yml' },
      ),
    );
    this.ssmCommandPassRole.grantPassRole(this.controllerRole);

    this.ssmCommandsSnsTopic = new SNSTopic(
      this.context,
      'virtual-desktop-controller-sns-topic',
      this.stack,
      {
        topicName: `${this.clusterName}-${this.moduleId}-ssm-commands-sns-topic`,
        displayName: `${this.clusterName}-${this.moduleId}-ssm-commands-topic`,
        masterKey: this.context.config.getString('cluster.sns.kms_key_id'),
      },
    );
    this.addCommonTags(this.ssmCommandsSnsTopic);
    this.ssmCommandsSnsTopic.addSubscription(
      new snsSubscriptions.SqsSubscription(this.controllerSqsQueue, {
        deadLetterQueue: this.controllerSqsQueueDlq,
      }),
    );
  }

  subscribeToEc2NotificationEvents(): void {
    const ec2EventSnsTopic = lookupEc2StateChangeTopic(this.context, this.stack);

    ec2EventSnsTopic.addSubscription(
      new snsSubscriptions.SqsSubscription(this.controllerSqsQueue, {
        deadLetterQueue: this.controllerSqsQueueDlq,
        filterPolicy: {
          [IDEA_TAG_MODULE_ID.replace(':', '_')]: sns.SubscriptionFilter.stringFilter({
            allowlist: [this.moduleId],
          }),
        },
      }),
    );
  }

  // --- scheduled events -------------------------------------------------------------------------

  buildScheduledEventNotificationInfra(): void {
    const lambdaName = `${this.moduleId}-scheduled-event-transformer`;
    this.scheduledEventTransformerLambdaRole = new Role(this.context, `${lambdaName}-role`, this.stack, {
      assumedBy: ['lambda'],
      description: `${lambdaName}-role`,
    });

    this.scheduledEventTransformerLambdaRole.attachInlinePolicy(
      new Policy(this.context, `${lambdaName}-policy`, this.stack, {
        policyTemplateName: 'controller-scheduled-event-transformer-lambda.yml',
      }),
    );

    const scheduledEventTransformerLambda = new LambdaFunction(this.context, lambdaName, this.stack, {
      description: `${this.moduleId} lambda to intercept all scheduled events and transform to the required event object.`,
      environment: { IDEA_CONTROLLER_EVENTS_QUEUE_URL: this.eventSqsQueue.queueUrl },
      timeoutSeconds: 180,
      role: this.scheduledEventTransformerLambdaRole,
      ideaCodeAsset: new IdeaCodeAsset('idea_controller_scheduled_event_transformer'),
    });

    this.addNagSuppression(
      [{ rule_id: 'AwsSolutions-L1', reason: 'Python Runtime is selected for stability.' }],
      scheduledEventTransformerLambda,
    );

    const scheduleTriggerRule = new events.Rule(
      this.stack,
      `${this.clusterName}-${this.moduleId}-schedule-rule`,
      {
        enabled: true,
        ruleName: `${this.clusterName}-${this.moduleId}-schedule-rule`,
        description: 'Event Rule to Trigger schedule check EVERY 30 minutes on VDC Controller',
        schedule: events.Schedule.cron({ minute: '0/30' }),
      },
    );

    scheduleTriggerRule.addTarget(new eventsTargets.LambdaFunction(scheduledEventTransformerLambda));

    // CloudFormation does not support tags on EventBridge rules in GovCloud.
    if (this.requiredString('cluster.aws.partition') !== AWS_PARTITION_GOVCLOUD) {
      this.addCommonTags(scheduleTriggerRule);
    }
  }

  // --- OAuth ------------------------------------------------------------------------------------

  buildOauth2Client(): void {
    const resourceServer = this.userPool.addResourceServer('resource-server', {
      identifier: this.moduleId,
      scopes: [
        new cognito.ResourceServerScope({ scopeName: 'read', scopeDescription: 'Allow Read Access' }),
        new cognito.ResourceServerScope({ scopeName: 'write', scopeDescription: 'Allow Write Access' }),
      ],
    });

    // DCV session manager external authentication.
    const sessionManagerResourceServer = this.userPool.addResourceServer(
      'dcv-session-manager-resource-server',
      {
        identifier: 'dcv-session-manager',
        scopes: [new cognito.ResourceServerScope({ scopeName: 'sm_scope', scopeDescription: 'sm_scope' })],
      },
    );

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
          cognito.OAuthScope.custom('dcv-session-manager/sm_scope'),
        ],
      },
      refreshTokenValidity: Duration.days(30),
      userPoolClientName: this.moduleId,
    });
    client.node.addDependency(sessionManagerResourceServer);
    client.node.addDependency(resourceServer);

    const oauthCredentialsLambdaArn = this.requiredString(
      'identity-provider.cognito.oauth_credentials_lambda_arn',
    );
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
      MODULE_VIRTUAL_DESKTOP_CONTROLLER,
      this.stack,
      client.userPoolClientId,
      clientSecret.getAttString('ClientSecret'),
    );
  }

  // --- DCV host (VDI) ---------------------------------------------------------------------------

  buildDcvHostInfra(): void {
    // A customer-managed policy so the same base permissions can be attached to project roles.
    this.dcvHostPolicy = new ManagedPolicy(
      this.context,
      `${this.moduleId}-${COMPONENT_DCV_HOST}-policy`,
      this.stack,
      {
        managedPolicyName: `${this.clusterName}-${this.awsRegion}-${this.moduleId}-${COMPONENT_DCV_HOST}`,
        description: `Permissions assigned to virtual-desktop-${COMPONENT_DCV_HOST}`,
        policyTemplateName: 'virtual-desktop-dcv-host.yml',
      },
    );
    this.dcvHostRole = this.buildIamRole(
      `IAM role assigned to virtual-desktop-${COMPONENT_DCV_HOST}`,
      COMPONENT_DCV_HOST,
    );
    this.dcvHostRole.addManagedPolicy(this.dcvHostPolicy);
    this.dcvHostRole.grantPassRole(this.controllerRole);

    this.dcvHostInstanceProfile = new InstanceProfile(
      this.context,
      `${this.moduleId}-${COMPONENT_DCV_HOST}-instance-profile`,
      this.stack,
      [this.dcvHostRole],
    );

    this.dcvHostSecurityGroup = new VirtualDesktopBastionAccessSecurityGroup(
      this.context,
      `${this.moduleId}-dcv-host-security-group`,
      this.stack,
      this.cluster.vpc,
      {
        bastionHostSecurityGroup: this.clusterSecurityGroup('bastion-host'),
        description: 'Security Group for DCV Host',
        directoryServiceAccess: true,
        componentName: 'DCV Host',
      },
    );
  }

  // --- DCV broker -------------------------------------------------------------------------------

  buildDcvBroker(): void {
    const clientTargetGroupArn = this.ecsEnabled
      ? this.ecsTargetGroupArn("dcv-broker", 0)
      : (() => {
          const targetGroup = new elbv2.ApplicationTargetGroup(
            this.stack,
            `${COMPONENT_DCV_BROKER}-client-target-group`,
            {
              port: this.brokerClientCommunicationPort,
              targetType: elbv2.TargetType.INSTANCE,
              protocol: elbv2.ApplicationProtocol.HTTPS,
              vpc: this.cluster.vpc,
              targetGroupName: this.getTargetGroupName(`${COMPONENT_DCV_BROKER}-c`),
            },
          );
          targetGroup.configureHealthCheck({ enabled: true, path: '/health' });
          return targetGroup.targetGroupArn;
        })();

    new CustomResource(this.stack, 'dcv-broker-client-endpoint', {
      serviceToken: this.clusterEndpointsLambdaArn,
      properties: {
        endpoint_name: 'broker-client-endpoint',
        listener_arn: this.requiredString(
          'cluster.load_balancers.internal_alb.dcv_broker_client_listener_arn',
        ),
        priority: 0,
        default_action: true,
        actions: [{ Type: 'forward', TargetGroupArn: clientTargetGroupArn }],
      },
      resourceType: 'Custom::DcvBrokerClientEndpointInternal',
    });

    const agentTargetGroupArn = this.ecsEnabled
      ? this.ecsTargetGroupArn("dcv-broker", 1)
      : (() => {
          const targetGroup = new elbv2.ApplicationTargetGroup(
            this.stack,
            `${COMPONENT_DCV_BROKER}-agent-target-group`,
            {
              port: this.brokerAgentCommunicationPort,
              targetType: elbv2.TargetType.INSTANCE,
              protocol: elbv2.ApplicationProtocol.HTTPS,
              vpc: this.cluster.vpc,
              targetGroupName: this.getTargetGroupName(`${COMPONENT_DCV_BROKER}-a`),
            },
          );
          targetGroup.configureHealthCheck({ enabled: true, path: '/health' });
          return targetGroup.targetGroupArn;
        })();

    new CustomResource(this.stack, 'dcv-broker-agent-endpoint', {
      serviceToken: this.clusterEndpointsLambdaArn,
      properties: {
        // The agent endpoint registers under the client endpoint's name; the name is the custom
        // resource's physical identity, so it stays as deployed.
        endpoint_name: 'broker-client-endpoint',
        listener_arn: this.requiredString(
          'cluster.load_balancers.internal_alb.dcv_broker_agent_listener_arn',
        ),
        priority: 0,
        default_action: true,
        actions: [{ Type: 'forward', TargetGroupArn: agentTargetGroupArn }],
      },
      resourceType: 'Custom::DcvBrokerAgentEndpointInternal',
    });

    const gatewayTargetGroupArn = this.ecsEnabled
      ? this.ecsTargetGroupArn("dcv-broker", 2)
      : (() => {
          const targetGroup = new elbv2.ApplicationTargetGroup(
            this.stack,
            `${COMPONENT_DCV_BROKER}-gateway-target-group`,
            {
              port: this.brokerGatewayCommunicationPort,
              targetType: elbv2.TargetType.INSTANCE,
              protocol: elbv2.ApplicationProtocol.HTTPS,
              vpc: this.cluster.vpc,
              targetGroupName: this.getTargetGroupName(`${COMPONENT_DCV_BROKER}-g`),
            },
          );
          targetGroup.configureHealthCheck({ enabled: true, path: '/health' });
          return targetGroup.targetGroupArn;
        })();

    new CustomResource(this.stack, 'dcv-broker-gateway-endpoint', {
      serviceToken: this.clusterEndpointsLambdaArn,
      properties: {
        endpoint_name: 'broker-gateway-endpoint',
        listener_arn: this.requiredString(
          'cluster.load_balancers.internal_alb.dcv_broker_gateway_listener_arn',
        ),
        priority: 0,
        default_action: true,
        actions: [{ Type: 'forward', TargetGroupArn: gatewayTargetGroupArn }],
      },
      resourceType: 'Custom::DcvBrokerGatewayEndpointInternal',
    });

    this.dcvBrokerSecurityGroup = new VirtualDesktopBrokerSecurityGroup(
      this.context,
      `${this.moduleId}-${COMPONENT_DCV_BROKER}-security-group`,
      this.stack,
      this.cluster.vpc,
      {
        bastionHostSecurityGroup: this.clusterSecurityGroup('bastion-host'),
        publicLoadbalancerSecurityGroup: this.clusterSecurityGroup('external-load-balancer'),
        description: 'Security Group for Virtual Desktop DCV Broker',
        componentName: 'DCV Broker',
      },
    );
    // The rule exists to reach a host. It goes when the last host does, not when routing moves.
    if (!this.hostsPresent) this.removeBastionHostIngressRule(this.dcvBrokerSecurityGroup);

    const dcvBrokerPackageUri = this.componentBootstrapPackageUri(
      'dcv_broker_bootstrap_package_uri',
      'dcv-broker',
    );
    const proxyConfig = this.proxyConfig();

    const brokerUserdata = buildBootstrapUserData({
      awsRegion: this.awsRegion,
      bootstrapPackageUri: dcvBrokerPackageUri,
      installCommands: ['/bin/bash dcv-broker/setup.sh'],
      infraConfig: {
        BROKER_CLIENT_TARGET_GROUP_ARN: '${__BROKER_CLIENT_TARGET_GROUP_ARN__}',
        CONTROLLER_EVENTS_QUEUE_URL: '${__CONTROLLER_EVENTS_QUEUE_URL__}',
      },
      proxyConfig,
      baseOs: this.requiredString('virtual-desktop-controller.dcv_broker.autoscaling.base_os'),
    });
    const substitutedUserdata = Fn.sub(brokerUserdata, {
      __BROKER_CLIENT_TARGET_GROUP_ARN__: clientTargetGroupArn,
      __CONTROLLER_EVENTS_QUEUE_URL__: this.eventSqsQueue.queueUrl,
    });

    this.dcvBrokerRole = this.buildIamRole(
      `IAM role assigned to virtual-desktop-${COMPONENT_DCV_BROKER}`,
      COMPONENT_DCV_BROKER,
      'virtual-desktop-dcv-broker.yml',
    );

    if (!this.hostsPresent) return;

    this.dcvBrokerAutoScalingGroup = this.buildAutoScalingGroup({
      componentName: COMPONENT_DCV_BROKER,
      securityGroup: this.dcvBrokerSecurityGroup,
      iamRole: this.dcvBrokerRole,
      substitutedUserdata,
      nodeType: NODE_TYPE_INFRA,
    });
    this.dcvBrokerAutoScalingGroup.node.addDependency(this.eventSqsQueue);

    // Under the container flag these ARNs are the container target groups, which take IP targets,
    // so a retained group registers with nothing and stands idle until a rollback recreates its own.
    if (this.ecsEnabled) return;

    // An ASG cannot be added to a second target group through the L2, so the L1 property is
    // written directly. The order is part of the deployed template.
    (
      this.dcvBrokerAutoScalingGroup.node.defaultChild as autoscaling.CfnAutoScalingGroup
    ).targetGroupArns = [
      agentTargetGroupArn,
      clientTargetGroupArn,
      gatewayTargetGroupArn,
    ];
  }

  // --- IAM --------------------------------------------------------------------------------------

  private buildIamRole(roleDescription: string, componentName: string, componentJinja?: string): Role {
    const ec2ManagedPolicies = this.getEc2InstanceManagedPolicies();

    const role = new Role(this.context, `${this.moduleId}-${componentName}-role`, this.stack, {
      description: roleDescription,
      assumedBy: ['ssm', 'ec2'],
      managedPolicies: ec2ManagedPolicies,
    });
    if (componentJinja === undefined) return role;
    role.attachInlinePolicy(
      new Policy(
        this.context,
        `${this.clusterName}-${this.moduleId}-${componentName}-policy`,
        this.stack,
        { policyTemplateName: componentJinja, vars: { role_arn: role.roleArn } },
      ),
    );
    return role;
  }

  // --- controller -------------------------------------------------------------------------------

  buildVirtualDesktopController(): void {
    const config = this.context.config;
    this.controllerSecurityGroup = new VirtualDesktopPublicLoadBalancerAccessSecurityGroup(
      this.context,
      `${this.moduleId}-${COMPONENT_CONTROLLER}-security-group`,
      this.stack,
      this.cluster.vpc,
      {
        bastionHostSecurityGroup: this.clusterSecurityGroup('bastion-host'),
        publicLoadbalancerSecurityGroup: this.clusterSecurityGroup('external-load-balancer'),
        description: 'Security Group for Virtual Desktop Controller',
        directoryServiceAccess: true,
        componentName: 'Virtual Desktop Controller',
      },
    );
    // The rule exists to reach a host. It goes when the last host does, not when routing moves.
    if (!this.hostsPresent) this.removeBastionHostIngressRule(this.controllerSecurityGroup);

    const controllerBootstrapPackageUri = this.componentBootstrapPackageUri(
      'controller_bootstrap_package_uri',
      'controller',
    );

    this.controllerRole = this.buildIamRole(
      `IAM role assigned to virtual-desktop-${COMPONENT_CONTROLLER}`,
      COMPONENT_CONTROLLER,
      'virtual-desktop-controller.yml',
    );

    const proxyConfig = this.proxyConfig();

    if (this.hostsPresent) {
      this.controllerAutoScalingGroup = this.buildAutoScalingGroup({
        componentName: COMPONENT_CONTROLLER,
        securityGroup: this.controllerSecurityGroup,
        iamRole: this.controllerRole,
        substitutedUserdata: Fn.sub(
          buildBootstrapUserData({
            awsRegion: this.awsRegion,
            bootstrapPackageUri: controllerBootstrapPackageUri,
            installCommands: ['/bin/bash virtual-desktop-controller/setup.sh'],
            proxyConfig,
            baseOs: this.requiredString('virtual-desktop-controller.controller.autoscaling.base_os'),
          }),
        ),
        nodeType: NODE_TYPE_APP,
      });

      this.controllerAutoScalingGroup.node.addDependency(this.eventSqsQueue);
      this.controllerAutoScalingGroup.node.addDependency(this.controllerSqsQueue);
    }

    const externalTargetGroupArn = this.ecsEnabled
      ? this.ecsTargetGroupArn("vdc", 0)
      : (() => {
          const targetGroup = new elbv2.ApplicationTargetGroup(
            this.stack,
            'controller-target-group-ext',
            {
              port: 8443,
              protocol: elbv2.ApplicationProtocol.HTTPS,
              protocolVersion: elbv2.ApplicationProtocolVersion.HTTP1,
              targetType: elbv2.TargetType.INSTANCE,
              vpc: this.cluster.vpc,
              targetGroupName: this.getTargetGroupName('vdc-ext'),
            },
          );
          targetGroup.configureHealthCheck({ enabled: true, path: '/healthcheck' });
          return targetGroup.targetGroupArn;
        })();

    new CustomResource(this.stack, 'controller-endpoint-ext', {
      serviceToken: this.clusterEndpointsLambdaArn,
      properties: {
        endpoint_name: `${this.moduleId}-controller-endpoint-ext`,
        listener_arn: this.requiredString('cluster.load_balancers.external_alb.https_listener_arn'),
        priority: requiredInt(config, 'virtual-desktop-controller.controller.endpoints.external.priority'),
        conditions: [
          {
            Field: 'path-pattern',
            Values: requiredList(
              this.context.config,
              'virtual-desktop-controller.controller.endpoints.external.path_patterns',
            ),
          },
        ],
        actions: [{ Type: 'forward', TargetGroupArn: externalTargetGroupArn }],
      },
      resourceType: 'Custom::ControllerEndpointExternal',
    });

    const internalTargetGroupArn = this.ecsEnabled
      ? this.ecsTargetGroupArn("vdc", 1)
      : (() => {
          const targetGroup = new elbv2.ApplicationTargetGroup(
            this.stack,
            'controller-target-group-int',
            {
              port: 8443,
              protocol: elbv2.ApplicationProtocol.HTTPS,
              protocolVersion: elbv2.ApplicationProtocolVersion.HTTP1,
              targetType: elbv2.TargetType.INSTANCE,
              vpc: this.cluster.vpc,
              targetGroupName: this.getTargetGroupName('vdc-int'),
            },
          );
          targetGroup.configureHealthCheck({ enabled: true, path: '/healthcheck' });
          return targetGroup.targetGroupArn;
        })();

    new CustomResource(this.stack, 'controller-endpoint-int', {
      serviceToken: this.clusterEndpointsLambdaArn,
      properties: {
        endpoint_name: `${this.moduleId}-controller-endpoint-int`,
        listener_arn: this.requiredString('cluster.load_balancers.internal_alb.https_listener_arn'),
        priority: requiredInt(config, 'virtual-desktop-controller.controller.endpoints.internal.priority'),
        conditions: [
          {
            Field: 'path-pattern',
            Values: requiredList(
              this.context.config,
              'virtual-desktop-controller.controller.endpoints.internal.path_patterns',
            ),
          },
        ],
        actions: [{ Type: 'forward', TargetGroupArn: internalTargetGroupArn }],
      },
      resourceType: 'Custom::ControllerEndpointInternal',
    });

    // Under the container flag these ARNs are the container target groups, which take IP targets,
    // so a retained group registers with nothing and stands idle until a rollback recreates its own.
    if (!this.ecsEnabled) {
      (
        this.controllerAutoScalingGroup.node.defaultChild as autoscaling.CfnAutoScalingGroup
      ).targetGroupArns = [internalTargetGroupArn, externalTargetGroupArn];
    }
  }

  // --- host groups ------------------------------------------------------------------------------

  private buildAutoScalingGroup(options: AutoScalingGroupOptions): autoscaling.AutoScalingGroup {
    const config = this.context.config;
    const { componentName } = options;

    const isPublic =
      config.getBool(this.componentKey(componentName, 'autoscaling.public'), false) &&
      this.cluster.publicSubnets.length > 0;
    const vpcSubnets: ec2.SubnetSelection = isPublic
      ? { subnets: this.cluster.publicSubnets }
      : { subnets: this.cluster.privateSubnets };

    const baseOs = this.requiredString(this.componentKey(componentName, 'autoscaling.base_os'));
    const blockDeviceName = ec2BlockDeviceName(baseOs);
    const blockDeviceTypeString = config.getString('virtual-desktop-controller.volume_type', 'gp3');
    const blockDeviceVolumeType =
      blockDeviceTypeString === 'gp3' ? ec2.EbsDeviceVolumeType.GP3 : ec2.EbsDeviceVolumeType.GP2;

    const enableDetailedMonitoring = config.getBool(
      this.componentKey(componentName, 'autoscaling.enable_detailed_monitoring'),
      false,
    );
    const metadataHttpTokens = this.requiredString(
      this.componentKey(componentName, 'autoscaling.metadata_http_tokens'),
    );

    const ebsKmsKey = lookupEbsKmsKey(this.context, this.stack, componentName);

    const launchTemplate = new ec2.LaunchTemplate(this.stack, `${componentName}-lt`, {
      instanceType: new ec2.InstanceType(
        this.requiredString(this.componentKey(componentName, 'autoscaling.instance_type')),
      ),
      machineImage: ec2.MachineImage.genericLinux({
        [this.awsRegion]: this.requiredString(
          this.componentKey(componentName, 'autoscaling.instance_ami'),
        ),
      }),
      securityGroup: options.securityGroup,
      userData: ec2.UserData.custom(options.substitutedUserdata),
      keyPair: lookupKeyPair(this.context, this.stack, `${componentName}-key-pair`),
      blockDevices: [
        {
          deviceName: blockDeviceName,
          volume: ec2.BlockDeviceVolume.ebs(
            config.getInt(this.componentKey(componentName, 'autoscaling.volume_size'), 200),
            { encrypted: true, kmsKey: ebsKmsKey, volumeType: blockDeviceVolumeType },
          ),
        },
      ],
      role: options.iamRole,
      requireImdsv2: metadataHttpTokens === 'required',
    });

    const autoScalingGroup = new autoscaling.AutoScalingGroup(this.stack, `${componentName}-asg`, {
      vpc: this.cluster.vpc,
      vpcSubnets,
      autoScalingGroupName: `${this.clusterName}-${this.moduleId}-${componentName}-asg`,
      launchTemplate,
      // `Monitoring.BASIC` is 0, which is why passing this alongside a launch template does not
      // trip the CDK guard. Detailed monitoring would throw at synth.
      instanceMonitoring: enableDetailedMonitoring
        ? autoscaling.Monitoring.DETAILED
        : autoscaling.Monitoring.BASIC,
      groupMetrics: [autoscaling.GroupMetrics.all()],
      minCapacity: config.getInt(this.componentKey(componentName, 'autoscaling.min_capacity'), 1),
      maxCapacity: config.getInt(this.componentKey(componentName, 'autoscaling.max_capacity'), 3),
      newInstancesProtectedFromScaleIn: config.getBool(
        this.componentKey(componentName, 'autoscaling.new_instances_protected_from_scale_in'),
        true,
      ),
      cooldown: Duration.minutes(
        config.getInt(this.componentKey(componentName, 'autoscaling.cooldown_minutes'), 5),
      ),
      healthChecks: autoscaling.HealthChecks.withAdditionalChecks({
        additionalTypes: [autoscaling.AdditionalHealthCheckType.ELB],
        gracePeriod: Duration.minutes(
          config.getInt(
            this.componentKey(componentName, 'autoscaling.elb_healthcheck.grace_time_minutes'),
            15,
          ),
        ),
      }),
      updatePolicy: autoscaling.UpdatePolicy.rollingUpdate({
        maxBatchSize: config.getInt(
          this.componentKey(componentName, 'autoscaling.rolling_update_policy.max_batch_size'),
          1,
        ),
        minInstancesInService: config.getInt(
          this.componentKey(
            componentName,
            'autoscaling.rolling_update_policy.min_instances_in_service',
          ),
          1,
        ),
        pauseTime: Duration.minutes(
          config.getInt(
            this.componentKey(componentName, 'autoscaling.rolling_update_policy.pause_time_minutes'),
            15,
          ),
        ),
      }),
      terminationPolicies: [autoscaling.TerminationPolicy.DEFAULT],
    });

    autoScalingGroup.scaleOnCpuUtilization('cpu-utilization-scaling-policy', {
      targetUtilizationPercent: config.getInt(
        this.componentKey(
          componentName,
          'autoscaling.cpu_utilization_scaling_policy.target_utilization_percent',
        ),
        80,
      ),
      estimatedInstanceWarmup: Duration.minutes(
        config.getInt(
          this.componentKey(
            componentName,
            'autoscaling.cpu_utilization_scaling_policy.estimated_instance_warmup_minutes',
          ),
          15,
        ),
      ),
    });

    Tags.of(autoScalingGroup).add(IDEA_TAG_NODE_TYPE, options.nodeType);
    Tags.of(autoScalingGroup).add(
      IDEA_TAG_NAME,
      `${this.clusterName}-${this.moduleId}-${componentName}`,
    );

    if (!enableDetailedMonitoring) {
      this.addNagSuppression(
        [
          {
            rule_id: 'AwsSolutions-EC28',
            reason: 'detailed monitoring is a configurable option to save costs',
          },
        ],
        autoScalingGroup,
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
      autoScalingGroup,
    );
    return autoScalingGroup;
  }

  // --- DCV connection gateway -------------------------------------------------------------------

  private buildDcvConnectionGatewayInstanceInfrastructure(): void {
    this.dcvConnectionGatewaySecurityGroup = new VirtualDesktopPublicLoadBalancerAccessSecurityGroup(
      this.context,
      `${this.moduleId}-${COMPONENT_DCV_CONNECTION_GATEWAY}-security-group`,
      this.stack,
      this.cluster.vpc,
      {
        bastionHostSecurityGroup: this.clusterSecurityGroup('bastion-host'),
        publicLoadbalancerSecurityGroup: this.clusterSecurityGroup('external-load-balancer'),
        description: 'Security Group for Virtual Desktop DCV Connection Gateway',
        directoryServiceAccess: false,
        componentName: 'DCV Connection Gateway',
      },
    );
    // The rule exists to reach a host. It goes when the last host does, not when routing moves.
    if (!this.hostsPresent) this.removeBastionHostIngressRule(this.dcvConnectionGatewaySecurityGroup);

    if (!this.hostsPresent) {
      this.dcvConnectionGatewayRole = this.buildIamRole(
        `IAM role assigned to virtual-desktop-${COMPONENT_DCV_CONNECTION_GATEWAY}`,
        COMPONENT_DCV_CONNECTION_GATEWAY,
        'virtual-desktop-dcv-connection-gateway.yml',
      );
      return;
    }

    const gatewayBootstrapPackageUri = this.componentBootstrapPackageUri(
      'dcv_connection_gateway_package_uri',
      'dcv-connection-gateway',
    );

    const proxyConfig = this.proxyConfig();

    const connectionGatewayUserdata = buildBootstrapUserData({
      awsRegion: this.awsRegion,
      bootstrapPackageUri: gatewayBootstrapPackageUri,
      installCommands: ['/bin/bash dcv-connection-gateway/setup.sh'],
      infraConfig: {
        CERTIFICATE_SECRET_ARN: '${__CERTIFICATE_SECRET_ARN__}',
        PRIVATE_KEY_SECRET_ARN: '${__PRIVATE_KEY_SECRET_ARN__}',
      },
      proxyConfig,
      baseOs: this.requiredString(
        'virtual-desktop-controller.dcv_connection_gateway.autoscaling.base_os',
      ),
    });

    const externalCertificateProvided = requiredBool(
      this.context.config,
      'virtual-desktop-controller.dcv_connection_gateway.certificate.provided',
    );
    const selfSignedCert = this.dcvConnectionGatewaySelfSignedCert;
    const substitutedUserdata = !externalCertificateProvided
      ? Fn.sub(connectionGatewayUserdata, {
          __CERTIFICATE_SECRET_ARN__: (selfSignedCert as CustomResource).getAttString(
            'certificate_secret_arn',
          ),
          __PRIVATE_KEY_SECRET_ARN__: (selfSignedCert as CustomResource).getAttString(
            'private_key_secret_arn',
          ),
        })
      : Fn.sub(connectionGatewayUserdata, {
          __CERTIFICATE_SECRET_ARN__: this.requiredString(
            'virtual-desktop-controller.dcv_connection_gateway.certificate.certificate_secret_arn',
          ),
          __PRIVATE_KEY_SECRET_ARN__: this.requiredString(
            'virtual-desktop-controller.dcv_connection_gateway.certificate.private_key_secret_arn',
          ),
        });

    this.dcvConnectionGatewayRole = this.buildIamRole(
      `IAM role assigned to virtual-desktop-${COMPONENT_DCV_CONNECTION_GATEWAY}`,
      COMPONENT_DCV_CONNECTION_GATEWAY,
      'virtual-desktop-dcv-connection-gateway.yml',
    );

    this.dcvConnectionGatewayAutoScalingGroup = this.buildAutoScalingGroup({
      componentName: COMPONENT_DCV_CONNECTION_GATEWAY,
      securityGroup: this.dcvConnectionGatewaySecurityGroup,
      iamRole: this.dcvConnectionGatewayRole,
      substitutedUserdata,
      nodeType: NODE_TYPE_INFRA,
    });
  }

  private buildDcvConnectionGatewayNetworkInfrastructure(): void {
    const config = this.context.config;
    const isPublic = config.getBool('cluster.load_balancers.external_alb.public', true);
    const externalNlbSubnets = isPublic ? this.cluster.publicSubnets : this.cluster.privateSubnets;
    this.externalNlb = new elbv2.NetworkLoadBalancer(
      this.stack,
      `${this.clusterName}-${this.moduleId}-external-nlb`,
      {
        loadBalancerName: `${this.clusterName}-${this.moduleId}-external-nlb`,
        vpc: this.cluster.vpc,
        internetFacing: isPublic,
        vpcSubnets: { subnets: externalNlbSubnets },
      },
    );

    if (config.getBool('virtual-desktop-controller.external_nlb.access_logs', false)) {
      const accessLogDestination = lookupClusterS3Bucket(this.context, this.stack);
      this.externalNlb.logAccessLogs(
        accessLogDestination,
        `logs/${this.moduleId}/external-nlb-access-logs`,
      );
    }

    const quicSupported = requiredBool(config, 'virtual-desktop-controller.dcv_session.quic_support');
    const protocol = quicSupported ? elbv2.Protocol.TCP_UDP : elbv2.Protocol.TCP;
    // TN: TCP network. TUN: TCP + UDP network.
    const tgSuffix = quicSupported ? 'TUN' : 'TN';

    let dcvConnectionGatewayTargetGroup: elbv2.INetworkTargetGroup;
    if (this.ecsEnabled) {
      dcvConnectionGatewayTargetGroup = elbv2.NetworkTargetGroup.fromTargetGroupAttributes(
        this.stack,
        'ecs-dcv-connection-gateway-target-group-nlb',
        {
          // The container stack publishes the one gateway target group that matches this
          // setting, so there is a single entry whichever protocol is in use.
          targetGroupArn: this.ecsTargetGroupArn("dcv-gateway", 0),
        },
      );
    } else {
      const targetGroup = new elbv2.NetworkTargetGroup(
        this.stack,
        'dcv-connection-gateway-target-group-nlb',
        {
          port: 8443,
          protocol,
          targetType: elbv2.TargetType.INSTANCE,
          vpc: this.cluster.vpc,
          targets: [this.dcvConnectionGatewayAutoScalingGroup],
          targetGroupName: this.getTargetGroupName(
            `${COMPONENT_DCV_CONNECTION_GATEWAY}-${tgSuffix}`,
          ),
          healthCheck: { port: '8989', protocol: elbv2.Protocol.TCP },
          connectionTermination: true,
        },
      );
      // Stickiness attributes have no construct property on a network target group.
      targetGroup.setAttribute('stickiness.enabled', 'true');
      targetGroup.setAttribute('stickiness.type', 'source_ip');
      dcvConnectionGatewayTargetGroup = targetGroup;
    }

    new elbv2.NetworkListener(this.externalNlb, 'dcv-connection-gateway-nlb-listener', {
      loadBalancer: this.externalNlb,
      protocol,
      port: 443,
      defaultAction: elbv2.NetworkListenerAction.forward([dcvConnectionGatewayTargetGroup]),
    });

    this.dcvConnectionGatewaySecurityGroup.addIngressRule(
      ec2.Peer.ipv4(this.cluster.vpc.vpcCidrBlock),
      ec2.Port.tcp(8989),
      'Allow TCP traffic access for HealthCheck to DCV Connection Gateway',
    );

    const clusterPrefixListId = this.requiredString('cluster.network.cluster_prefix_list_id');
    this.dcvConnectionGatewaySecurityGroup.addIngressRule(
      ec2.Peer.prefixList(clusterPrefixListId),
      ec2.Port.allTraffic(),
      'Allow all Traffic access from Cluster Prefix List to DCV Connection Gateway',
    );

    for (const prefixListId of config.getList<string>('cluster.network.prefix_list_ids', [])) {
      this.dcvConnectionGatewaySecurityGroup.addIngressRule(
        ec2.Peer.prefixList(prefixListId),
        ec2.Port.allTraffic(),
        'Allow all traffic access from Prefix List to DCV Connection Gateway',
      );
    }
  }

  private buildSelfSignedCertForDcvConnectionGateway(): void {
    const selfSignedCertificateLambdaArn = this.requiredString(
      'cluster.self_signed_certificate_lambda_arn',
    );
    this.dcvConnectionGatewaySelfSignedCert = new CustomResource(
      this.stack,
      `${this.clusterName}-${this.moduleId}-external-cert-${COMPONENT_DCV_CONNECTION_GATEWAY}`,
      {
        serviceToken: selfSignedCertificateLambdaArn,
        properties: {
          domain_name: `${this.moduleId}.${this.clusterName}.idea.default`,
          certificate_name: `${this.clusterName}-${this.moduleId}-${COMPONENT_DCV_CONNECTION_GATEWAY}-certificate`,
          create_acm_certificate: false,
          kms_key_id: this.context.config.getString('cluster.secretsmanager.kms_key_id'),
          tags: {
            Name: `${this.clusterName}-${this.moduleId}-${COMPONENT_DCV_CONNECTION_GATEWAY} Self Signed Certificate`,
            'idea:ClusterName': this.clusterName,
            'idea:ModuleName': MODULE_VIRTUAL_DESKTOP_CONTROLLER,
          },
        },
        resourceType: 'Custom::SelfSignedCertificateConnectionGateway',
      },
    );
  }

  buildDcvConnectionGateway(): void {
    const externalCertificateProvided = requiredBool(
      this.context.config,
      'virtual-desktop-controller.dcv_connection_gateway.certificate.provided',
    );
    if (!externalCertificateProvided) {
      this.buildSelfSignedCertForDcvConnectionGateway();
    }

    this.buildDcvConnectionGatewayInstanceInfrastructure();
    this.buildDcvConnectionGatewayNetworkInfrastructure();
  }

  // --- backups ----------------------------------------------------------------------------------

  buildBackups(): void {
    // A boolean `false` renders as `False`, a non-empty string that passes this guard.
    const clusterBackupsEnabled = this.context.config.getString('cluster.backups.enabled');
    if (!clusterBackupsEnabled) return;

    const vdiHostBackupEnabled = this.context.config.getBool(
      'virtual-desktop-controller.vdi_host_backup.enabled',
      false,
    );
    if (!vdiHostBackupEnabled) return;

    const backupRole = lookupBackupRole(this.context, this.stack);
    const backupVault = lookupClusterBackupVault(this.context, this.stack);

    const backupPlanConfig = this.context.config.getConfig(
      'virtual-desktop-controller.vdi_host_backup.backup_plan',
    );

    this.backupPlan = new BackupPlan(this.stack, {
      backupPlanName: `${this.clusterName}-${this.moduleId}`,
      backupPlanConfig,
      backupVault,
      backupRole,
    });
  }

  // --- cluster settings -------------------------------------------------------------------------

  buildClusterSettings(): void {
    const config = this.context.config;
    const clusterSettings: Record<string, unknown> = {
      deployment_id: this.deploymentId,
      client_id: this.oauth2ClientSecret.clientId.ref,
      client_secret: this.oauth2ClientSecret.clientSecret.ref,
      dcv_host_security_group_id: this.dcvHostSecurityGroup.securityGroupId,
      dcv_host_role_arn: this.dcvHostRole.roleArn,
      dcv_host_role_name: this.dcvHostRole.roleName,
      dcv_host_role_id: this.dcvHostRole.roleId,
      dcv_host_policy_arn: this.dcvHostPolicy.managedPolicyArn,
      dcv_broker_role_arn: this.dcvBrokerRole.roleArn,
      dcv_broker_role_name: this.dcvBrokerRole.roleName,
      dcv_broker_role_id: this.dcvBrokerRole.roleId,
      scheduled_event_transformer_lambda_role_arn: this.scheduledEventTransformerLambdaRole.roleArn,
      scheduled_event_transformer_lambda_role_name: this.scheduledEventTransformerLambdaRole.roleName,
      scheduled_event_transformer_lambda_role_id: this.scheduledEventTransformerLambdaRole.roleId,
      dcv_host_instance_profile_arn: instanceProfileArn(this.context, this.dcvHostInstanceProfile.ref),
      ssm_commands_sns_topic_arn: this.ssmCommandsSnsTopic.topicArn,
      ssm_commands_sns_topic_name: this.ssmCommandsSnsTopic.topicName,
      ssm_commands_pass_role_arn: this.ssmCommandPassRole.roleArn,
      ssm_commands_pass_role_id: this.ssmCommandPassRole.roleId,
      ssm_commands_pass_role_name: this.ssmCommandPassRole.roleName,
      controller_iam_role_arn: this.controllerRole.roleArn,
      controller_iam_role_name: this.controllerRole.roleName,
      controller_iam_role_id: this.controllerRole.roleId,
      events_sqs_queue_url: this.eventSqsQueue.queueUrl,
      events_sqs_queue_arn: this.eventSqsQueue.queueArn,
      controller_sqs_queue_url: this.controllerSqsQueue.queueUrl,
      controller_sqs_queue_arn: this.controllerSqsQueue.queueArn,
      'external_nlb.load_balancer_dns_name': this.externalNlb.loadBalancerDnsName,
      'controller.asg_name': this.ecsEnabled
        ? this.ecsServiceName("vdc")
        : this.controllerAutoScalingGroup.autoScalingGroupName,
      'controller.asg_arn': this.ecsEnabled
        ? this.requiredString("ecs.vdc.service_arn")
        : this.controllerAutoScalingGroup.autoScalingGroupArn,
      'dcv_broker.asg_name': this.ecsEnabled
        ? this.ecsServiceName("dcv-broker")
        : this.dcvBrokerAutoScalingGroup.autoScalingGroupName,
      'dcv_broker.asg_arn': this.ecsEnabled
        ? this.requiredString("ecs.dcv-broker.service_arn")
        : this.dcvBrokerAutoScalingGroup.autoScalingGroupArn,
      'dcv_connection_gateway.asg_name': this.ecsEnabled
        ? this.ecsServiceName("dcv-gateway")
        : this.dcvConnectionGatewayAutoScalingGroup.autoScalingGroupName,
      'dcv_connection_gateway.asg_arn': this.ecsEnabled
        ? this.requiredString("ecs.dcv-gateway.service_arn")
        : this.dcvConnectionGatewayAutoScalingGroup.autoScalingGroupArn,
    };

    if (
      !config.getBool('virtual-desktop-controller.dcv_connection_gateway.certificate.provided', false)
    ) {
      const selfSignedCert = this.dcvConnectionGatewaySelfSignedCert as CustomResource;
      clusterSettings['dcv_connection_gateway.certificate.certificate_secret_arn'] =
        selfSignedCert.getAttString('certificate_secret_arn');
      clusterSettings['dcv_connection_gateway.certificate.private_key_secret_arn'] =
        selfSignedCert.getAttString('private_key_secret_arn');
    } else {
      clusterSettings['dcv_connection_gateway.certificate.provided'] = this.requiredString(
        'virtual-desktop-controller.dcv_connection_gateway.certificate.provided',
      );
      clusterSettings['dcv_connection_gateway.certificate.certificate_secret_arn'] =
        this.requiredString(
          'virtual-desktop-controller.dcv_connection_gateway.certificate.certificate_secret_arn',
        );
      clusterSettings['dcv_connection_gateway.certificate.private_key_secret_arn'] =
        this.requiredString(
          'virtual-desktop-controller.dcv_connection_gateway.certificate.private_key_secret_arn',
        );
      clusterSettings['dcv_connection_gateway.certificate.custom_dns_name'] = this.requiredString(
        'virtual-desktop-controller.dcv_connection_gateway.certificate.custom_dns_name',
      );
    }

    if (this.backupPlan !== undefined) {
      clusterSettings['vdi_host_backup.backup_plan.arn'] = this.backupPlan.getBackupPlanArn();
    }

    // The controller's iam:PassRole for project roles is granted at deploy time when bedrock is
    // enabled, so the setting is written under the same gate and the web portal can tell a
    // redeploy is owed.
    const clusterManagerModuleId = config.moduleId(MODULE_CLUSTER_MANAGER);
    if (config.getBool(`${clusterManagerModuleId}.bedrock.enabled`, false)) {
      clusterSettings['bedrock.project_pass_role_arn'] = this.arnBuilder.getProjectRoleArn();
    }

    this.updateClusterSettings(clusterSettings);
  }
}

export function buildStack(props: StackBuildProps): void {
  new VirtualDesktopControllerStack(props);
}
