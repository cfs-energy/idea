/**
 * Three EC2 host groups (controller, DCV broker, DCV connection gateway), the external network
 * load balancer the gateway sits behind, the DCV host (VDI) identity used by session instances,
 * the SQS/SNS plumbing the controller listens on, and the scheduled-event transformer Lambda.
 *
 * Under the container flag this stack also runs the controller, the broker and the gateway as
 * tasks on the container stack's capacity. They are built here because each one reads this
 * module's own settings rows at boot, so it must not start before this stack has written them,
 * and because the gateway's target group needs the load balancer this stack owns.
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

import { Aws, CustomResource, Duration, Fn, RemovalPolicy, Tags } from 'aws-cdk-lib';
import * as autoscaling from 'aws-cdk-lib/aws-autoscaling';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as events from 'aws-cdk-lib/aws-events';
import * as eventsTargets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as servicediscovery from 'aws-cdk-lib/aws-servicediscovery';
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
import {
  STREAM_PREFIX_APPLICATION,
  STREAM_PREFIX_BROKER,
  STREAM_PREFIX_GATEWAY,
  addLogTailContainer,
  addStorageMounts,
  adoptedLogDriver,
  applicationTargetGroup,
  applicationContainerSettings,
  attachApplicationFileLogs,
  buildEc2Service,
  buildExecutionRole,
  grantInjectedSecret,
  buildTaskDefinition,
  buildTaskRole,
  commonEnvironment,
  containerImage,
  dockerLabels,
  healthCheckGrace,
  requiredInt as requiredEcsInt,
  roleSizing,
  serviceArn,
  type ContainerScope,
} from '../constructs/container.ts';

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

/** The gateway writes its log files here; a sidecar tails them into the agent-created group. */
const GATEWAY_LOG_DIRECTORY = '/var/log/dcv-connection-gateway';
/** The broker writes its log files here; a sidecar tails them into the agent-created group. */
const BROKER_LOG_DIRECTORY = '/var/log/dcv-session-manager-broker';

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
  /** The task roles, when the components run as tasks; the settings below name whichever runs. */
  private controllerTaskRole?: Role;
  private dcvBrokerTaskRole?: Role;
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
  /** Present under the container flag: the three task services on the container stack's capacity. */
  controllerService: ecs.Ec2Service | undefined;
  dcvBrokerService: ecs.Ec2Service | undefined;
  dcvConnectionGatewayService: ecs.Ec2Service | undefined;
  private controllerTargetGroups: elbv2.ApplicationTargetGroup[] = [];
  private brokerTargetGroups: elbv2.ApplicationTargetGroup[] = [];
  /** The broker port each entry of `brokerTargetGroups` fronts, in the same order. */
  private brokerTargetGroupPorts: number[] = [];
  private controllerEndpoints: CustomResource[] = [];
  private brokerEndpoints: CustomResource[] = [];

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
    this.buildContainerServices();
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

  /** The input the shared container helpers take. */
  private get containerScope(): ContainerScope {
    return {
      ctx: this.context,
      stack: this.stack,
      vpc: this.cluster.vpc,
      privateSubnets: this.cluster.privateSubnets,
    };
  }

  /**
   * Service names, fixed so the settings resource can publish them without a reference. This stack
   * runs three services, so each carries its component.
   */
  private componentServiceName(component: string): string {
    return `${this.clusterName}-${this.moduleId}-${component}`;
  }

  /** The controller's two IP target groups, created beside the endpoints that route to them. */
  private buildControllerTargetGroups(): void {
    if (!this.ecsEnabled) return;
    const scope = this.containerScope;
    this.controllerTargetGroups = (['e', 'i'] as const).map((suffix) =>
      applicationTargetGroup(scope, {
        constructId: `vdc-ecs-${suffix}-target-group`,
        targetGroupName: this.getTargetGroupName(`vdc-ecs-${suffix}`),
        port: 8443,
        healthCheckPath: '/healthcheck',
      }),
    );
  }

  /** The broker's three IP target groups, one per broker listener on the internal load balancer. */
  private buildBrokerTargetGroups(): void {
    if (!this.ecsEnabled) return;
    const scope = this.containerScope;
    const ports: Array<[string, number]> = [
      ['c', this.brokerClientCommunicationPort],
      ['a', this.brokerAgentCommunicationPort],
      ['g', this.brokerGatewayCommunicationPort],
    ];
    this.brokerTargetGroupPorts = ports.map(([, port]) => port);
    this.brokerTargetGroups = ports.map(([suffix, port]) =>
      applicationTargetGroup(scope, {
        constructId: `brk-ecs-${suffix}-target-group`,
        targetGroupName: this.getTargetGroupName(`brk-ecs-${suffix}`),
        port,
        healthCheckPath: '/health',
      }),
    );
  }

  /**
   * The controller and broker tasks on the container stack's capacity.
   *
   * Both start after the endpoints that give their target groups a load balancer, and after this
   * stack's settings resource, because each application reads its own module's rows at boot. Each
   * runs in the host security group of the component it replaces, so the task holds the network
   * position the host holds.
   */
  private buildContainerServices(): void {
    if (!this.ecsEnabled) return;
    this.controllerService = this.buildControllerService();
    this.dcvBrokerService = this.buildDcvBrokerService();
  }

  private buildControllerService(): ecs.Ec2Service {
    const scope = this.containerScope;
    const sizing = roleSizing(scope, 'vdc');
    const { role: taskRole, policy: taskPolicy } = buildTaskRole(scope, {
      constructId: 'controller-task-role',
      name: `${this.moduleId}-${COMPONENT_CONTROLLER}-task-role`,
      description: `IAM role assigned to the virtual-desktop-${COMPONENT_CONTROLLER} task`,
      managedPolicyArns: this.getEc2InstanceManagedPolicies(),
      policyConstructId: `${this.clusterName}-${this.moduleId}-${COMPONENT_CONTROLLER}-task-policy`,
      policyTemplateName: 'virtual-desktop-controller.yml',
    });
    this.controllerTaskRole = taskRole;
    const executionRole = buildExecutionRole(
      scope,
      'controller-task-execution-role',
      `${this.moduleId}-${COMPONENT_CONTROLLER}-task-execution-role`,
    );
    const taskDefinition = buildTaskDefinition(scope, 'controller-task-definition', {
      executionRole,
      taskRole,
    });
    const logGroupName = `/${this.clusterName}/${this.moduleId}/controller`;
    const container = taskDefinition.addContainer('controller-container', {
      ...applicationContainerSettings('vdc'),
      cpu: sizing.cpu,
      dockerLabels: dockerLabels(scope, 'vdc'),
      environment: commonEnvironment(scope, {
        role: 'vdc',
        moduleId: this.moduleId,
        moduleName: MODULE_VIRTUAL_DESKTOP_CONTROLLER,
      }),
      image: containerImage(scope),
      logging: adoptedLogDriver(scope, 'controller-log-group', logGroupName, STREAM_PREFIX_APPLICATION),
      memoryLimitMiB: sizing.memory,
    });
    container.addPortMappings({ containerPort: 8443, protocol: ecs.Protocol.TCP });
    addStorageMounts(scope, taskDefinition, container);
    attachApplicationFileLogs(scope, { idPrefix: 'controller', logGroupName, taskDefinition, container });

    const service = buildEc2Service(scope, {
      constructId: 'controller-service',
      serviceName: this.componentServiceName(COMPONENT_CONTROLLER),
      taskDefinition,
      desiredCount: sizing.desired,
      securityGroups: [this.controllerSecurityGroup],
      healthCheckGracePeriod: healthCheckGrace('vdc'),
      dependencies: [taskRole, taskPolicy, executionRole, ...this.controllerEndpoints],
    });
    for (const targetGroup of this.controllerTargetGroups) service.attachToApplicationTargetGroup(targetGroup);
    return service;
  }

  private buildDcvBrokerService(): ecs.Ec2Service {
    const scope = this.containerScope;
    const sizing = roleSizing(scope, 'dcv-broker');
    const { role: taskRole, policy: taskPolicy } = buildTaskRole(scope, {
      constructId: 'dcv-broker-task-role',
      name: `${this.moduleId}-${COMPONENT_DCV_BROKER}-task-role`,
      description: `IAM role assigned to the virtual-desktop-${COMPONENT_DCV_BROKER} task`,
      managedPolicyArns: this.getEc2InstanceManagedPolicies(),
      policyConstructId: `${this.clusterName}-${this.moduleId}-${COMPONENT_DCV_BROKER}-task-policy`,
      policyTemplateName: 'virtual-desktop-dcv-broker.yml',
    });
    this.dcvBrokerTaskRole = taskRole;
    // The broker role script reads its ports from cluster-settings; the host bootstrap rendered
    // them into the configuration file, so the shared policy template never granted the table.
    taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:GetItem'],
        resources: [this.arnBuilder.getDdbTableArn('cluster-settings')],
      }),
    );
    const executionRole = buildExecutionRole(
      scope,
      'dcv-broker-task-execution-role',
      `${this.moduleId}-${COMPONENT_DCV_BROKER}-task-execution-role`,
    );
    const taskDefinition = buildTaskDefinition(scope, 'dcv-broker-task-definition', {
      executionRole,
      taskRole,
    });
    const logGroupName = `/${this.clusterName}/${this.moduleId}/dcv-broker`;
    const namespaceName = this.requiredString('ecs.namespace_name');
    const container = taskDefinition.addContainer('dcv-broker-container', {
      ...applicationContainerSettings('dcv-broker', this.brokerTargetGroupPorts),
      cpu: sizing.cpu,
      dockerLabels: dockerLabels(scope, 'dcv-broker'),
      environment: {
        ...commonEnvironment(scope, {
          role: 'dcv-broker',
          moduleId: this.moduleId,
          moduleName: MODULE_VIRTUAL_DESKTOP_CONTROLLER,
        }),
        IDEA_COGNITO_PROVIDER_URL: this.requiredString('identity-provider.cognito.provider_url'),
        IDEA_SERVICE_DISCOVERY_NAME: `vdc-broker.${namespaceName}`,
        // The task network namespace has no second address family, so a dual-stack JVM fails to
        // create its sockets. The virtual machine reads this variable itself at startup, which is
        // why it is this name and not one the vendor launcher would have to pass on.
        JAVA_TOOL_OPTIONS: '-Djava.net.preferIPv4Stack=true',
      },
      image: containerImage(scope),
      logging: adoptedLogDriver(scope, 'dcv-broker-log-group', logGroupName, STREAM_PREFIX_BROKER),
      memoryLimitMiB: sizing.memory,
    });
    for (const port of [
      this.brokerClientCommunicationPort,
      this.brokerAgentCommunicationPort,
      this.brokerGatewayCommunicationPort,
    ]) {
      container.addPortMappings({ containerPort: port, protocol: ecs.Protocol.TCP });
    }
    taskDefinition.addVolume({ name: 'broker-logs' });
    container.addMountPoints({
      containerPath: BROKER_LOG_DIRECTORY,
      readOnly: false,
      sourceVolume: 'broker-logs',
    });
    addStorageMounts(scope, taskDefinition, container);
    addLogTailContainer(scope, taskDefinition, {
      containerId: 'dcv-broker-file-logs',
      directories: [BROKER_LOG_DIRECTORY],
      logGroupName,
      logGroupConstructId: 'dcv-broker-file-log-group',
      streamPrefix: STREAM_PREFIX_BROKER,
      sourceVolume: 'broker-logs',
      containerPath: BROKER_LOG_DIRECTORY,
      readOnly: true,
    });

    const namespace = servicediscovery.PrivateDnsNamespace.fromPrivateDnsNamespaceAttributes(
      this.stack,
      'ecs-service-discovery-namespace',
      {
        namespaceArn: `arn:${Aws.PARTITION}:servicediscovery:${Aws.REGION}:${Aws.ACCOUNT_ID}:namespace/${this.requiredString('ecs.namespace_id')}`,
        namespaceId: this.requiredString('ecs.namespace_id'),
        namespaceName,
      },
    );
    const service = buildEc2Service(scope, {
      constructId: 'dcv-broker-service',
      serviceName: this.componentServiceName(COMPONENT_DCV_BROKER),
      taskDefinition,
      desiredCount: sizing.desired,
      securityGroups: [this.dcvBrokerSecurityGroup],
      healthCheckGracePeriod: healthCheckGrace('dcv-broker'),
      cloudMapOptions: {
        cloudMapNamespace: namespace,
        dnsRecordType: servicediscovery.DnsRecordType.A,
        dnsTtl: Duration.seconds(10),
        failureThreshold: 1,
        name: 'vdc-broker',
      },
      dependencies: [taskRole, taskPolicy, executionRole, ...this.brokerEndpoints],
    });
    // Each target group fronts one broker port. `attachToApplicationTargetGroup` registers every
    // group on the container's first mapping, the client port, and the session-manager agent and
    // the gateway are then answered on the wrong port: the agent logs "JSON Error: EOF" and no
    // desktop ever reaches READY.
    this.brokerTargetGroups.forEach((targetGroup, index) => {
      targetGroup.addTarget(
        service.loadBalancerTarget({
          containerName: 'dcv-broker-container',
          containerPort: this.brokerTargetGroupPorts[index] as number,
        }),
      );
    });
    return service;
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
    // The controller task passes the same roles the controller host did; only the bedrock
    // project roles come through the policy template.
    if (this.controllerTaskRole !== undefined) this.ssmCommandPassRole.grantPassRole(this.controllerTaskRole);

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

    // the client returns its own generated secret, so nothing has to read it back
    const clientSecret = (client.node.defaultChild as cognito.CfnUserPoolClient).attrClientSecret;

    this.oauth2ClientSecret = new OAuthClientIdAndSecret(
      this.context,
      this.moduleId,
      MODULE_VIRTUAL_DESKTOP_CONTROLLER,
      this.stack,
      client.userPoolClientId,
      clientSecret,
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
    if (this.controllerTaskRole !== undefined) this.dcvHostRole.grantPassRole(this.controllerTaskRole);

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
    this.buildBrokerTargetGroups();
    const clientTargetGroupArn = this.ecsEnabled
      ? (this.brokerTargetGroups[0] as elbv2.ApplicationTargetGroup).targetGroupArn
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

    this.brokerEndpoints.push(new CustomResource(this.stack, 'dcv-broker-client-endpoint', {
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
    }));

    const agentTargetGroupArn = this.ecsEnabled
      ? (this.brokerTargetGroups[1] as elbv2.ApplicationTargetGroup).targetGroupArn
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

    this.brokerEndpoints.push(new CustomResource(this.stack, 'dcv-broker-agent-endpoint', {
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
    }));

    const gatewayTargetGroupArn = this.ecsEnabled
      ? (this.brokerTargetGroups[2] as elbv2.ApplicationTargetGroup).targetGroupArn
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

    this.brokerEndpoints.push(new CustomResource(this.stack, 'dcv-broker-gateway-endpoint', {
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
    }));

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

    this.buildControllerTargetGroups();
    const externalTargetGroupArn = this.ecsEnabled
      ? (this.controllerTargetGroups[0] as elbv2.ApplicationTargetGroup).targetGroupArn
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

    this.controllerEndpoints.push(new CustomResource(this.stack, 'controller-endpoint-ext', {
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
    }));

    const internalTargetGroupArn = this.ecsEnabled
      ? (this.controllerTargetGroups[1] as elbv2.ApplicationTargetGroup).targetGroupArn
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

    this.controllerEndpoints.push(new CustomResource(this.stack, 'controller-endpoint-int', {
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
    }));

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

    const certificateSecrets = this.dcvConnectionGatewayCertificateSecretArns();
    const substitutedUserdata = Fn.sub(connectionGatewayUserdata, {
      __CERTIFICATE_SECRET_ARN__: certificateSecrets.certificate,
      __PRIVATE_KEY_SECRET_ARN__: certificateSecrets.privateKey,
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
        crossZoneEnabled: this.ecsEnabled ? true : undefined,
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

    let dcvConnectionGatewayTargetGroup: elbv2.NetworkTargetGroup;
    if (this.ecsEnabled) {
      // The task is an IP target, so it cannot share the host group's target group, whose target
      // type is fixed at creation. The name differs as well: the two groups exist together for the
      // moment the change set creates one before it deletes the other.
      dcvConnectionGatewayTargetGroup = new elbv2.NetworkTargetGroup(
        this.stack,
        'dcv-connection-gateway-ecs-target-group-nlb',
        {
          port: 8443,
          protocol,
          targetType: elbv2.TargetType.IP,
          vpc: this.cluster.vpc,
          targetGroupName: this.getTargetGroupName(`gw-ecs-${tgSuffix}`),
          healthCheck: { port: '8989', protocol: elbv2.Protocol.TCP, interval: Duration.seconds(5), timeout: Duration.seconds(4), healthyThresholdCount: 2, unhealthyThresholdCount: 2 },
          deregistrationDelay: Duration.seconds(300),
          connectionTermination: true,
        },
      );
      dcvConnectionGatewayTargetGroup.setAttribute('stickiness.enabled', 'true');
      dcvConnectionGatewayTargetGroup.setAttribute('stickiness.type', 'source_ip');
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

    if (this.ecsEnabled) {
      this.buildDcvConnectionGatewayService(dcvConnectionGatewayTargetGroup);
    }
  }

  /**
   * The certificate pair the gateway serves. The rows hold the operator's certificate when one is
   * provided and the pair the deploy tool generated or adopted when not, so both cases read the
   * same two keys.
   */
  private dcvConnectionGatewayCertificateSecretArns(): { certificate: string; privateKey: string } {
    const prefix = 'virtual-desktop-controller.dcv_connection_gateway.certificate';
    return {
      certificate: this.requiredString(`${prefix}.certificate_secret_arn`),
      privateKey: this.requiredString(`${prefix}.private_key_secret_arn`),
    };
  }

  /**
   * The gateway container service.
   *
   * A service may not name a target group that has no load balancer, and the load balancer is
   * created here, so the service is too. What it needs from the container stack it reads from the
   * settings that stack published: the cluster, the capacity provider, the image and the processor
   * architecture. It runs in the gateway host security group, so it holds the network position the
   * host holds, and it reads the certificate pair the host reads at boot.
   *
   * The gateway is a proxy. It mounts no shared storage and reads no application settings, so the
   * task carries only what its role script consumes.
   */
  private buildDcvConnectionGatewayService(targetGroup: elbv2.NetworkTargetGroup): void {
    const scope = this.containerScope;
    const { role: taskRole, policy: taskPolicy } = buildTaskRole(scope, {
      constructId: 'dcv-connection-gateway-task-role',
      name: `${this.moduleId}-${COMPONENT_DCV_CONNECTION_GATEWAY}-task-role`,
      description: `IAM role assigned to the virtual-desktop-${COMPONENT_DCV_CONNECTION_GATEWAY} task`,
      managedPolicyArns: this.getEc2InstanceManagedPolicies(),
      policyConstructId: `${this.clusterName}-${this.moduleId}-${COMPONENT_DCV_CONNECTION_GATEWAY}-task-policy`,
      policyTemplateName: 'virtual-desktop-dcv-connection-gateway.yml',
    });
    const executionRole = buildExecutionRole(
      scope,
      'dcv-connection-gateway-task-execution-role',
      `${this.moduleId}-${COMPONENT_DCV_CONNECTION_GATEWAY}-task-execution-role`,
    );
    const taskDefinition = buildTaskDefinition(scope, 'dcv-connection-gateway-task-definition', {
      executionRole,
      taskRole,
    });

    // Agent-created on every cluster with hosts, and adopted by the container stack before this
    // stack deploys, so it is written to by name.
    const logGroupName = `/${this.clusterName}/${this.moduleId}/dcv-connection-gateway`;
    const certificateSecrets = this.dcvConnectionGatewayCertificateSecretArns();
    grantInjectedSecret(scope, executionRole, certificateSecrets.certificate);
    grantInjectedSecret(scope, executionRole, certificateSecrets.privateKey);
    const container = taskDefinition.addContainer('dcv-connection-gateway-container', {
      ...applicationContainerSettings('dcv-gateway'),
      cpu: requiredEcsInt(scope, 'ecs.tasks.dcv-gateway.cpu'),
      dockerLabels: dockerLabels(scope, 'dcv-gateway'),
      environment: {
        AWS_DEFAULT_REGION: this.awsRegion,
        IDEA_CLUSTER_NAME: this.clusterName,
        IDEA_CONTAINER_ROLE: 'dcv-gateway',
        IDEA_INTERNAL_ALB_ENDPOINT: `https://${this.requiredString(
          'cluster.load_balancers.internal_alb.load_balancer_dns_name',
        )}`,
      },
      image: containerImage(scope),
      logging: adoptedLogDriver(scope, 'dcv-connection-gateway-log-group', logGroupName, STREAM_PREFIX_GATEWAY),
      memoryLimitMiB: requiredEcsInt(scope, 'ecs.tasks.dcv-gateway.memory'),
      secrets: {
        DCV_GATEWAY_CERT_PEM: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretCompleteArn(
            this.stack,
            'dcv-connection-gateway-certificate-secret',
            certificateSecrets.certificate,
          ),
        ),
        DCV_GATEWAY_KEY_PEM: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretCompleteArn(
            this.stack,
            'dcv-connection-gateway-private-key-secret',
            certificateSecrets.privateKey,
          ),
        ),
      },
    });
    // The gateway serves both stream protocols on 8443, but a container port may appear in only
    // one mapping. Under `awsvpc` the task owns its network interface and the mapping does not
    // filter traffic, so one mapping publishes the port and the security group admits each
    // protocol. The health port 8989 is separate and unaffected.
    container.addPortMappings({ containerPort: 8443, protocol: ecs.Protocol.TCP });
    taskDefinition.addVolume({ name: 'gateway-logs' });
    container.addMountPoints({
      containerPath: GATEWAY_LOG_DIRECTORY,
      readOnly: false,
      sourceVolume: 'gateway-logs',
    });
    // The awslogs driver carries stdout only; the sidecar follows the gateway's files.
    addLogTailContainer(scope, taskDefinition, {
      containerId: 'dcv-connection-gateway-file-logs',
      directories: [GATEWAY_LOG_DIRECTORY],
      logGroupName,
      logGroupConstructId: 'dcv-connection-gateway-file-log-group',
      streamPrefix: STREAM_PREFIX_GATEWAY,
      sourceVolume: 'gateway-logs',
      containerPath: GATEWAY_LOG_DIRECTORY,
      readOnly: true,
    });

    const service = buildEc2Service(scope, {
      constructId: 'dcv-connection-gateway-service',
      serviceName: this.componentServiceName(COMPONENT_DCV_CONNECTION_GATEWAY),
      taskDefinition,
      desiredCount: requiredEcsInt(scope, 'ecs.tasks.dcv-gateway.desired'),
      securityGroups: [this.dcvConnectionGatewaySecurityGroup],
      healthCheckGracePeriod: healthCheckGrace('dcv-gateway'),
      dependencies: [taskRole, taskPolicy, executionRole],
    });
    // Attaching also makes the service depend on the listener, which is what gives the target
    // group its load balancer: a service may not name a target group that has none.
    service.attachToNetworkTargetGroup(targetGroup);
    this.dcvConnectionGatewayService = service;
  }

  /**
   * The deploy tool generates this pair now (`src/cli/certificates.ts`) and publishes the two ARNs
   * as settings rows. The resource stays for one release so a cluster that has it can update it in
   * place to the Node handler, and it carries `Retain` so nothing here can destroy the pair the
   * running gateway is serving. Remove it in the release after this one, once every cluster has
   * deployed this one.
   */
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
        removalPolicy: RemovalPolicy.RETAIN,
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
      // The controller authenticates queue messages by the sender's role id, including its own
      // and the broker's. Under containers those senders are the task roles.
      dcv_broker_role_id: (this.dcvBrokerTaskRole ?? this.dcvBrokerRole).roleId,
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
      controller_iam_role_id: (this.controllerTaskRole ?? this.controllerRole).roleId,
      events_sqs_queue_url: this.eventSqsQueue.queueUrl,
      events_sqs_queue_arn: this.eventSqsQueue.queueArn,
      controller_sqs_queue_url: this.controllerSqsQueue.queueUrl,
      controller_sqs_queue_arn: this.controllerSqsQueue.queueArn,
      'external_nlb.load_balancer_dns_name': this.externalNlb.loadBalancerDnsName,
      // Literals under the container flag: each service has to start after this resource, because
      // its application reads its own rows from it, so this resource must not reference a service.
      'controller.asg_name': this.ecsEnabled
        ? this.componentServiceName(COMPONENT_CONTROLLER)
        : this.controllerAutoScalingGroup.autoScalingGroupName,
      'controller.asg_arn': this.ecsEnabled
        ? serviceArn(this.containerScope, this.componentServiceName(COMPONENT_CONTROLLER))
        : this.controllerAutoScalingGroup.autoScalingGroupArn,
      'dcv_broker.asg_name': this.ecsEnabled
        ? this.componentServiceName(COMPONENT_DCV_BROKER)
        : this.dcvBrokerAutoScalingGroup.autoScalingGroupName,
      'dcv_broker.asg_arn': this.ecsEnabled
        ? serviceArn(this.containerScope, this.componentServiceName(COMPONENT_DCV_BROKER))
        : this.dcvBrokerAutoScalingGroup.autoScalingGroupArn,
      'dcv_connection_gateway.asg_name': this.ecsEnabled
        ? this.componentServiceName(COMPONENT_DCV_CONNECTION_GATEWAY)
        : this.dcvConnectionGatewayAutoScalingGroup.autoScalingGroupName,
      'dcv_connection_gateway.asg_arn': this.ecsEnabled
        ? serviceArn(this.containerScope, this.componentServiceName(COMPONENT_DCV_CONNECTION_GATEWAY))
        : this.dcvConnectionGatewayAutoScalingGroup.autoScalingGroupArn,
    };

    if (
      !config.getBool('virtual-desktop-controller.dcv_connection_gateway.certificate.provided', false)
    ) {
      const certificateSecrets = this.dcvConnectionGatewayCertificateSecretArns();
      clusterSettings['dcv_connection_gateway.certificate.certificate_secret_arn'] = certificateSecrets.certificate;
      clusterSettings['dcv_connection_gateway.certificate.private_key_secret_arn'] = certificateSecrets.privateKey;
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

    const settings = this.updateClusterSettings(clusterSettings);
    // Each task reads `client_id`, `client_secret` and the rest of this module's rows at boot, so
    // none of them may start before they are written.
    for (const service of [this.controllerService, this.dcvBrokerService, this.dcvConnectionGatewayService]) {
      service?.node.addDependency(settings);
    }
  }
}

export function buildStack(props: StackBuildProps): void {
  new VirtualDesktopControllerStack(props);
}
