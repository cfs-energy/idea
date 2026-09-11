/**
 * Container control-plane stack.
 *
 * This stack supplies shared ECS capacity and runs the five host-module roles
 * as arm64 tasks. It owns the task ENI security groups, while the host security
 * group is limited to the container instances.
 */

import { Annotations, Aws, CustomResource, Duration, Fn, RemovalPolicy } from "aws-cdk-lib";
import type { StackBuildProps } from "../app.ts";
import { IdeaBaseStack } from "../base-stack.ts";
import { isDsActivedirectory } from "../constructs/base.ts";
import { LOG_RETENTION_DAYS, Policy } from "../constructs/common.ts";
import { ExistingSocaCluster } from "../constructs/existing-resources.ts";
import { buildTrimmedResourceName } from "../../util/names.ts";
import * as autoscaling from "aws-cdk-lib/aws-autoscaling";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as efs from "aws-cdk-lib/aws-efs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Provider } from "aws-cdk-lib/custom-resources";

const CONTAINER_ROLES = ["cluster-manager", "vdc", "scheduler", "dcv-broker", "dcv-gateway"] as const;

type ContainerRole = (typeof CONTAINER_ROLES)[number];

/** Stream family preserved from the agent `application_{ip}` prefix, minus the address. */
const STREAM_PREFIX_APPLICATION = "application";
/** Stream family for OpenPBS files that used `server_logs_`, `sched_logs_`, and `accounting_logs_`. */
const STREAM_PREFIX_OPENPBS = "openpbs";
/** Stream family preserved from the agent `dcv-session-manager-broker_{ip}` prefix. */
const STREAM_PREFIX_BROKER = "dcv-session-manager-broker";
/** Stream family preserved from the agent `dcv-connection-gateway_{ip}` prefix. */
const STREAM_PREFIX_GATEWAY = "dcv-connection-gateway";
/** Stream family for the optional host daemon. */
const STREAM_PREFIX_DATADOG = "datadog";
/** Agent-created groups use `cluster.cloudwatch_logs.retention_in_days`, which defaults to 90. */
const DEFAULT_AGENT_LOG_RETENTION_DAYS = 90;

/**
 * Start allowances, used for the load balancer health check grace and for the container health
 * check that runs alongside it.
 *
 * The library default grace is 60 seconds, which is shorter than every role's start: the first
 * deployment would then fail its target health checks while the process being checked is still
 * starting, and the circuit breaker would roll it back. The two wait ceilings below are the ones
 * the image's role scripts impose on themselves, so they are facts about the start rather than
 * estimates: `roles/scheduler.sh` waits up to 30 attempts 5 seconds apart for the batch server to
 * answer, and `roles/broker.sh` waits up to 30 attempts 2 seconds apart for service discovery to
 * resolve. The application allowance is the time a role needs after its own wait to answer a health
 * request, and on a scheduler first start it also covers creating a new state directory and its
 * datastore.
 */
const PBS_SERVER_WAIT_SECONDS = 150;
const BROKER_DISCOVERY_WAIT_SECONDS = 60;
const APPLICATION_START_SECONDS = 120;
/**
 * The load balancer grace has to cover registration as well as the start, because a target that has
 * not yet passed its consecutive successful checks is not healthy yet and the grace is what keeps the
 * platform from acting on that. These target groups use the library health check, which is five
 * checks thirty seconds apart for the application load balancer and three for the network one, so the
 * application figure is the longer of the two.
 */
const TARGET_REGISTRATION_SECONDS = 150;

/** Task policy template per role: the same template the module's instance role renders. */
const TASK_POLICY_TEMPLATES: Readonly<Record<ContainerRole, string>> = {
  "cluster-manager": "cluster-manager.yml",
  vdc: "virtual-desktop-controller.yml",
  scheduler: "scheduler.yml",
  "dcv-broker": "virtual-desktop-dcv-broker.yml",
  "dcv-gateway": "virtual-desktop-dcv-connection-gateway.yml",
};

/** Egress every module host security group carries, for IPv4 and IPv6. */
const TCP_EGRESS_DESCRIPTION = "Allow all egress for TCP";
/** The ingress and egress pair the host groups add on an Active Directory cluster. */
const DIRECTORY_SERVICE_INGRESS_DESCRIPTION = "Allow UDP Traffic from VPC. Required for Directory Service";
const DIRECTORY_SERVICE_EGRESS_DESCRIPTION = "Allow UDP Traffic. Required for Directory Service";
/**
 * The container-optimised host image, resolved at launch. The x86_64 image has no architecture
 * segment in its parameter path and the arm64 one does, which is why this is a function of the
 * architecture rather than a string with a slot in it.
 */
function hostImageParameter(architecture: ec2.InstanceArchitecture): string {
  const prefix = "/aws/service/ecs/optimized-ami/amazon-linux-2023";
  return architecture === ec2.InstanceArchitecture.ARM_64
    ? `${prefix}/arm64/recommended/image_id`
    : `${prefix}/recommended/image_id`;
}

/** The task definition value for one host architecture. */
function cpuArchitecture(architecture: ec2.InstanceArchitecture): string {
  return architecture === ec2.InstanceArchitecture.ARM_64 ? "ARM64" : "X86_64";
}
/** PBS state and logs live on the scheduler task volume. */
const SCHEDULER_PBS_HOME = "/var/spool/pbs";
const APPLICATION_LOG_DIRECTORY = "/opt/idea/app/logs";
const BROKER_LOG_DIRECTORY = "/var/log/dcv-session-manager-broker";
const GATEWAY_LOG_DIRECTORY = "/var/log/dcv-connection-gateway";

/**
 * Create-or-adopt handler used by the ECS stack. Delete is a no-op so history
 * survives a stack rollback. Retention is applied only when the property is set.
 */
const ENSURE_AGENT_LOG_GROUP_HANDLER = `
import boto3

def handler(event, context):
    name = event["ResourceProperties"]["LogGroupName"]
    if event["RequestType"] != "Delete":
        logs = boto3.client("logs")
        try:
            logs.create_log_group(logGroupName=name)
        except logs.exceptions.ResourceAlreadyExistsException:
            pass
        retention = event["ResourceProperties"].get("RetentionInDays")
        if retention:
            logs.put_retention_policy(logGroupName=name, retentionInDays=int(retention))
    return {"PhysicalResourceId": name, "Data": {"LogGroupName": name}}
`.trim();

/**
 * Releases the host group's scale-in protection when the group is being deleted.
 *
 * Managed termination protection is what stops a scale-in killing a task mid-flight, and enabling
 * it requires scale-in protection on the group. Nothing removes that protection when the group is
 * meant to go away, so the group sits at desired zero with every instance still in service and
 * CloudFormation waits out its own timeout. This runs on Delete, which is the only moment our code
 * is in the loop during a rollback, and it clears both the group default and the instances that
 * carry the flag already. A group that has gone, or that never launched, is not an error.
 */
const RELEASE_SCALE_IN_PROTECTION_HANDLER = `
import boto3

def handler(event, context):
    name = event["ResourceProperties"]["AutoScalingGroupName"]
    if event["RequestType"] == "Delete":
        autoscaling = boto3.client("autoscaling")
        groups = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[name])
        for group in groups.get("AutoScalingGroups", []):
            autoscaling.update_auto_scaling_group(
                AutoScalingGroupName=name, NewInstancesProtectedFromScaleIn=False
            )
            instances = [
                instance["InstanceId"]
                for instance in group.get("Instances", [])
                if instance.get("ProtectedFromScaleIn")
            ]
            if instances:
                autoscaling.set_instance_protection(
                    AutoScalingGroupName=name,
                    InstanceIds=instances,
                    ProtectedFromScaleIn=False,
                )
    return {"PhysicalResourceId": "scale-in-protection-" + name}
`.trim();

interface RoleSizing {
  cpu: number;
  memory: number;
  desired: number;
}

interface StorageMount {
  readonly hostPath?: string;
  readonly fileSystemId?: string;
  readonly mountPath: string;
  readonly name: string;
}

interface RoleResources {
  readonly service: ecs.Ec2Service;
  readonly targetGroups: Array<elbv2.ApplicationTargetGroup | elbv2.NetworkTargetGroup>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export class EcsStack extends IdeaBaseStack {
  readonly cluster: ExistingSocaCluster;
  readonly ecsCluster: ecs.Cluster;
  readonly namespace: servicediscovery.PrivateDnsNamespace;
  readonly hostSecurityGroup: ec2.SecurityGroup;
  readonly hostRole: iam.Role;
  readonly hostAutoScalingGroup: autoscaling.AutoScalingGroup;
  readonly capacityProvider: ecs.AsgCapacityProvider;
  /** Clears the host group's scale-in protection when the group is deleted. */
  private scaleInRelease!: CustomResource;
  readonly executionRole: iam.Role;
  readonly gatewayExecutionRole: iam.Role;

  private readonly roleResources: Partial<Record<ContainerRole, RoleResources>> = {};
  /** Resolved once from the configured host family: the host image and every task follow it. */
  private readonly hostArchitecture: ec2.InstanceArchitecture;
  /**
   * Task identities this stack creates. The module instance roles stay where they are: one set
   * belongs to the hosts until they retire, the other to the tasks.
   */
  private readonly taskRoles: Partial<Record<ContainerRole, iam.Role>> = {};
  private readonly taskSecurityGroups: Partial<Record<ContainerRole, ec2.SecurityGroup>> = {};
  private instanceManagedPolicies: iam.IManagedPolicy[] | undefined;
  private readonly gatewayCertificateSecretArn: string;
  private readonly gatewayPrivateKeySecretArn: string;
  private readonly ensuredLogGroups = new Map<string, CustomResource>();
  private logGroupEnsureProvider: Provider | undefined;

  constructor(props: StackBuildProps) {
    super({
      scope: props.app,
      ctx: props.ctx,
      moduleName: props.moduleName,
      deploymentId: props.deploymentId,
      terminationProtection: props.terminationProtection,
      env: props.env,
    });

    this.cluster = new ExistingSocaCluster(this.context, this.stack);
    const execLogGroupName = this.execCommandLogGroupName();
    const execLogGroup = this.ensureAgentLogGroup("exec-log-group-ensure", execLogGroupName);
    this.ecsCluster = new ecs.Cluster(this.stack, "ecs-cluster", {
      clusterName: `${this.clusterName}-ecs`,
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
      executeCommandConfiguration: {
        logConfiguration: {
          cloudWatchLogGroup: logs.LogGroup.fromLogGroupName(this.stack, "exec-log-group-ref", execLogGroupName),
        },
        logging: ecs.ExecuteCommandLogging.OVERRIDE,
      },
      vpc: this.cluster.vpc,
    });
    // The group has to exist before a session opens: a cluster naming a group that is not there
    // runs sessions that are simply never recorded, and being attributable is the whole reason
    // this path is preferred to reaching a container host and using the runtime directly.
    this.ecsCluster.node.addDependency(execLogGroup);
    this.namespace = new servicediscovery.PrivateDnsNamespace(this.stack, "service-discovery-namespace", {
      name: `${this.clusterName}.ecs.local`,
      vpc: this.cluster.vpc,
    });
    this.hostArchitecture = this.hostInstanceType().architecture;
    const gatewaySecrets = this.loadGatewayCertificateSecrets();
    this.gatewayCertificateSecretArn = gatewaySecrets.certificateSecretArn;
    this.gatewayPrivateKeySecretArn = gatewaySecrets.privateKeySecretArn;

    this.hostSecurityGroup = this.buildHostSecurityGroup();
    this.hostRole = this.buildHostRole();
    this.hostAutoScalingGroup = this.buildHostAutoScalingGroup();
    this.capacityProvider = new ecs.AsgCapacityProvider(this.stack, "host-capacity-provider", {
      autoScalingGroup: this.hostAutoScalingGroup,
      capacityProviderName: `${this.clusterName}-ecs-capacity`,
      enableManagedScaling: true,
      enableManagedTerminationProtection: true,
      targetCapacityPercent: 100,
    });
    this.ecsCluster.addAsgCapacityProvider(this.capacityProvider);
    this.releaseScaleInProtectionOnDelete();

    this.executionRole = this.buildExecutionRole("ecs-task-execution-role");
    this.gatewayExecutionRole = this.buildExecutionRole("gateway-task-execution-role");
    this.buildApplicationServices();
    this.buildEndpoints();
    this.buildDatadogService();
    this.buildClusterSettings();
  }

  /** Returns a required string setting under the ECS module. */
  private requiredString(key: string): string {
    return this.context.config.getString(key, undefined, { required: true }) as string;
  }

  /** Returns a required integer setting under the ECS module. */
  private requiredInt(key: string): number {
    const value = this.context.config.getInt(key);
    if (value === undefined) throw new Error(`${key} is required for ECS`);
    return value;
  }

  /** Module id the application process reads from IDEA_MODULE_ID and from log group paths. */
  private ideaModuleId(role: ContainerRole): string {
    if (role === "cluster-manager") return this.context.config.moduleId("cluster-manager");
    if (role === "scheduler") return this.context.config.moduleId("scheduler");
    return this.context.config.moduleId("virtual-desktop-controller");
  }

  /** Module name the application process reads from IDEA_MODULE_NAME. */
  private ideaModuleName(role: ContainerRole): string {
    if (role === "cluster-manager") return "cluster-manager";
    if (role === "scheduler") return "scheduler";
    return "virtual-desktop-controller";
  }

  /**
   * Log group the CloudWatch agent writes to. Cluster-manager and scheduler use `/{cluster}/{module-id}`.
   * VDC components append `/controller`, `/dcv-broker`, or `/dcv-connection-gateway`.
   */
  private agentLogGroupName(role: ContainerRole, component?: string): string {
    const moduleId = this.ideaModuleId(role);
    return component === undefined ? `/${this.clusterName}/${moduleId}` : `/${this.clusterName}/${moduleId}/${component}`;
  }

  /**
   * Where command-execution sessions are recorded. Sits under the same `/{cluster}` prefix the
   * adopted agent groups use, so it is covered by the ensure provider's policy and by the cluster
   * retention, and is never deleted by a rollback.
   */
  private execCommandLogGroupName(): string {
    return `/${this.clusterName}/${this.moduleId}/exec`;
  }

  /** Retention copied from the CloudWatch agent setting. Invalid values leave an adopted group unchanged. */
  private agentLogRetentionDays(): number | undefined {
    const retentionInDays = this.context.config.getInt(
      "cluster.cloudwatch_logs.retention_in_days",
      DEFAULT_AGENT_LOG_RETENTION_DAYS,
    );
    return retentionInDays in LOG_RETENTION_DAYS ? retentionInDays : undefined;
  }

  /**
   * Provider that creates a missing group, adopts an existing one, sets retention,
   * and never deletes. CloudFormation does not own the group, so an upgrade cannot
   * fail with already-exists and a rollback cannot wipe history.
   */
  private agentLogGroupProvider(): Provider {
    if (this.logGroupEnsureProvider !== undefined) return this.logGroupEnsureProvider;

    const onEvent = new lambda.Function(this.stack, "agent-log-group-fn", {
      code: lambda.Code.fromInline(ENSURE_AGENT_LOG_GROUP_HANDLER),
      handler: "index.handler",
      runtime: lambda.Runtime.PYTHON_3_13,
      timeout: Duration.seconds(60),
    });
    onEvent.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["logs:CreateLogGroup", "logs:PutRetentionPolicy"],
        resources: [
          `arn:${Aws.PARTITION}:logs:${Aws.REGION}:${Aws.ACCOUNT_ID}:log-group:/${this.clusterName}`,
          `arn:${Aws.PARTITION}:logs:${Aws.REGION}:${Aws.ACCOUNT_ID}:log-group:/${this.clusterName}/*`,
        ],
      }),
    );
    this.logGroupEnsureProvider = new Provider(this.stack, "agent-log-group-provider", {
      onEventHandler: onEvent,
    });
    return this.logGroupEnsureProvider;
  }

  /** Ensures one agent log group exists with the cluster retention, then returns it. */
  private ensureAgentLogGroup(constructId: string, logGroupName: string): CustomResource {
    const existing = this.ensuredLogGroups.get(logGroupName);
    if (existing !== undefined) return existing;

    const properties: Record<string, string> = { LogGroupName: logGroupName };
    const retentionInDays = this.agentLogRetentionDays();
    if (retentionInDays !== undefined) properties["RetentionInDays"] = String(retentionInDays);

    const resource = new CustomResource(this.stack, constructId, {
      properties,
      serviceToken: this.agentLogGroupProvider().serviceToken,
    });
    this.ensuredLogGroups.set(logGroupName, resource);
    return resource;
  }

  /**
   * Writes to a preserved group without emitting AWS::Logs::LogGroup.
   * Stream names become `{prefix}/{container}/{task-id}`.
   */
  private adoptedLogDriver(constructId: string, logGroupName: string, streamPrefix: string): ecs.LogDriver {
    this.ensureAgentLogGroup(`${constructId}-ensure`, logGroupName);
    const logGroup = logs.LogGroup.fromLogGroupName(this.stack, `${constructId}-ref`, logGroupName);
    return ecs.LogDrivers.awsLogs({ logGroup, streamPrefix });
  }

  /** Keeps the task from starting before its log group has been created or adopted. */
  private bindContainerToLogGroup(container: ecs.ContainerDefinition, logGroupName: string): void {
    const resource = this.ensuredLogGroups.get(logGroupName);
    if (resource !== undefined) container.node.addDependency(resource);
  }

  /** Follows log files that the awslogs driver cannot tail. */
  private fileTailScript(directories: string[]): string {
    const mkdirLines = directories.map((directory) => `install -d -m 0755 "${directory}"`).join("\n");
    const globList = directories.map((directory) => `"${directory}"/*.log`).join(" ");
    return [
      "set -euo pipefail",
      mkdirLines,
      "while true; do",
      "  shopt -s nullglob",
      `  files=(${globList})`,
      "  if ((${#files[@]})); then exec tail -F -- \"${files[@]}\"; fi",
      "  sleep 5",
      "done",
    ].join("\n");
  }

  /**
   * Sidecar that tails the role's log files into a preserved group.
   * The main container still has its own awslogs driver for stdout.
   */
  private addLogTailContainer(
    taskDefinition: ecs.Ec2TaskDefinition,
    input: {
      constructId: string;
      containerId: string;
      directories: string[];
      logGroupName: string;
      streamPrefix: string;
      sourceVolume: string;
      containerPath: string;
      readOnly: boolean;
    },
  ): void {
    const container = taskDefinition.addContainer(input.containerId, {
      command: [this.fileTailScript(input.directories)],
      entryPoint: ["/bin/bash", "-lc"],
      essential: false,
      image: ecs.ContainerImage.fromRegistry(this.requiredString("ecs.image")),
      logging: this.adoptedLogDriver(input.constructId, input.logGroupName, input.streamPrefix),
      memoryReservationMiB: 32,
    });
    container.addMountPoints({
      containerPath: input.containerPath,
      readOnly: input.readOnly,
      sourceVolume: input.sourceVolume,
    });
    this.bindContainerToLogGroup(container, input.logGroupName);
  }

  /** Shared volume plus sidecar so application.log reaches the preserved group. */
  private attachApplicationFileLogs(
    role: "cluster-manager" | "vdc" | "scheduler",
    taskDefinition: ecs.Ec2TaskDefinition,
    container: ecs.ContainerDefinition,
  ): void {
    const logGroupName = this.agentLogGroupName(role, role === "vdc" ? "controller" : undefined);
    taskDefinition.addVolume({ name: "application-logs" });
    container.addMountPoints({
      containerPath: APPLICATION_LOG_DIRECTORY,
      readOnly: false,
      sourceVolume: "application-logs",
    });
    this.addLogTailContainer(taskDefinition, {
      constructId: `${role}-application-logs`,
      containerId: `${role}-application-logs`,
      directories: [APPLICATION_LOG_DIRECTORY],
      logGroupName,
      streamPrefix: STREAM_PREFIX_APPLICATION,
      sourceVolume: "application-logs",
      containerPath: APPLICATION_LOG_DIRECTORY,
      readOnly: true,
    });
  }

  /**
   * Creates the task ENI group for one role.
   *
   * The group re-establishes the network position the module's host security group holds, rule by
   * rule, using only the VPC the earlier cluster stack created. Nothing here reads a group an
   * application stack publishes later.
   *
   * Two host rules have no task counterpart. SSH from the bastion group is one: a task runs no ssh
   * daemon, and the module stacks already remove that rule when the container flag is on. The
   * separate `8443` allowance for the external load balancer group is the other: it is a subset of
   * the API rule below, because every load balancer sits in this VPC.
   *
   * Where a host rule opens every port and protocol from the VPC, the task rule names the ports the
   * task actually listens on instead. The controller and the gateway are the two host groups with
   * such a rule, and their listeners are `8443` for the API, `8443` over UDP for the gateway's QUIC
   * transport, and `8989` for the gateway health check.
   */
  private taskSecurityGroup(role: ContainerRole): ec2.SecurityGroup {
    const existing = this.taskSecurityGroups[role];
    if (existing !== undefined) return existing;

    const securityGroup = new ec2.SecurityGroup(this.stack, `${role}-task-security-group`, {
      allowAllOutbound: false,
      description: `Security group for the ${role} ECS task`,
      securityGroupName: this.buildResourceName(`ecs-${role}-task-security-group`),
      vpc: this.cluster.vpc,
    });
    const vpcPeer = ec2.Peer.ipv4(this.cluster.vpc.vpcCidrBlock);
    const apiIngress = (): void => {
      securityGroup.addIngressRule(
        vpcPeer,
        ec2.Port.tcp(8443),
        "Allow HTTP traffic from all VPC nodes for API access",
      );
    };

    if (role === "dcv-broker") {
      // The broker group is the one host group with no API rule: it is reached on the three broker
      // ports, and the brokers reach each other on the three fixed discovery ports.
      securityGroup.addIngressRule(vpcPeer, ec2.Port.tcpRange(8444, 8446), "Allow VPC to broker ports 8444-8446");
      for (const port of [47100, 47200, 47500]) {
        securityGroup.addIngressRule(securityGroup, ec2.Port.tcp(port), `Allow broker to broker port ${port}`);
      }
    } else {
      apiIngress();
    }
    if (role === "scheduler") {
      // The batch server and its execution hosts use reserved and ephemeral ports in both
      // directions, which is why the host group allows every TCP port from the VPC.
      securityGroup.addIngressRule(
        vpcPeer,
        ec2.Port.tcpRange(0, 65535),
        "Allow all TCP traffic from VPC to scheduler",
      );
    }
    if (role === "dcv-gateway") {
      securityGroup.addIngressRule(
        vpcPeer,
        ec2.Port.udp(8443),
        "Allow UDP traffic from all VPC nodes for the QUIC transport",
      );
      securityGroup.addIngressRule(
        vpcPeer,
        ec2.Port.tcp(8989),
        "Allow TCP traffic access for HealthCheck to DCV Connection Gateway",
      );
      this.addGatewayClientIngress(securityGroup);
    }

    securityGroup.addEgressRule(ec2.Peer.ipv4("0.0.0.0/0"), ec2.Port.tcpRange(0, 65535), TCP_EGRESS_DESCRIPTION);
    securityGroup.addEgressRule(ec2.Peer.ipv6("::/0"), ec2.Port.tcpRange(0, 65535), TCP_EGRESS_DESCRIPTION);
    if (role === "dcv-gateway" && this.quicSupported()) {
      // The gateway group carries this pair on a cluster with the QUIC transport on, because the
      // gateway then reaches each desktop over UDP.
      securityGroup.addEgressRule(
        ec2.Peer.ipv4("0.0.0.0/0"),
        ec2.Port.udpRange(0, 65535),
        "Allow all egress for UDP for QUIC Support on DCV Connection Gateway",
      );
      securityGroup.addEgressRule(
        ec2.Peer.ipv6("::/0"),
        ec2.Port.udpRange(0, 65535),
        "Allow all egress for UDP for QUIC Support on DCV Connection Gateway",
      );
    }
    // The host groups of the roles that join the directory carry this pair, and the task joins the
    // same way. The broker and the gateway host groups do not, so neither do their tasks.
    if (role !== "dcv-broker" && role !== "dcv-gateway" && isDsActivedirectory(this.context)) {
      securityGroup.addIngressRule(vpcPeer, ec2.Port.udpRange(0, 1024), DIRECTORY_SERVICE_INGRESS_DESCRIPTION);
      securityGroup.addEgressRule(
        ec2.Peer.ipv4("0.0.0.0/0"),
        ec2.Port.udpRange(0, 1024),
        DIRECTORY_SERVICE_EGRESS_DESCRIPTION,
      );
      securityGroup.addEgressRule(
        ec2.Peer.ipv6("::/0"),
        ec2.Port.udpRange(0, 1024),
        DIRECTORY_SERVICE_EGRESS_DESCRIPTION,
      );
    }

    this.addCommonTags(securityGroup);
    this.taskSecurityGroups[role] = securityGroup;
    return securityGroup;
  }

  /** True when the desktop module serves sessions over the QUIC transport. */
  private quicSupported(): boolean {
    return this.context.config.getBool("virtual-desktop-controller.dcv_session.quic_support", false);
  }

  /**
   * Allows desktop clients to reach the gateway task from the prefix lists the host group allows.
   *
   * A network load balancer target group of type TCP and UDP preserves the client address and cannot
   * be told not to, so the client address, not a load balancer address, is what the task sees. The
   * peers are the cluster prefix list the cluster stack maintains plus any prefix list the operator
   * added, which is exactly the set the host group allows all traffic from. The ports are the
   * gateway's two listener ports rather than all traffic.
   */
  private addGatewayClientIngress(securityGroup: ec2.SecurityGroup): void {
    const clusterPrefixListId = this.context.config.getString("cluster.network.cluster_prefix_list_id");
    const operatorPrefixListIds = this.context.config.getList<string>("cluster.network.prefix_list_ids", []);
    if (clusterPrefixListId === undefined || clusterPrefixListId === "") {
      Annotations.of(this.stack).addWarning(
        "cluster.network.cluster_prefix_list_id is not set, so the gateway task allows desktop clients from inside the VPC only. A cluster deployed by this tool always has the setting; a synthesis without it is not a deployable cluster.",
      );
    }
    const prefixListIds = clusterPrefixListId === undefined || clusterPrefixListId === ""
      ? operatorPrefixListIds
      : [clusterPrefixListId, ...operatorPrefixListIds];
    for (const prefixListId of prefixListIds) {
      securityGroup.addIngressRule(
        ec2.Peer.prefixList(prefixListId),
        ec2.Port.tcp(8443),
        "Allow TCP traffic access from Prefix List to DCV Connection Gateway",
      );
      securityGroup.addIngressRule(
        ec2.Peer.prefixList(prefixListId),
        ec2.Port.udp(8443),
        "Allow UDP traffic access from Prefix List to DCV Connection Gateway",
      );
    }
  }

  /** Builds the egress-only group attached to container-instance ENIs. */
  private buildHostSecurityGroup(): ec2.SecurityGroup {
    const securityGroup = new ec2.SecurityGroup(this.stack, "ecs-host-security-group", {
      allowAllOutbound: true,
      description: "Security group for ECS container hosts",
      securityGroupName: this.buildResourceName("ecs-host-security-group"),
      vpc: this.cluster.vpc,
    });
    this.addCommonTags(securityGroup);
    return securityGroup;
  }

  /** Builds the role used by ECS container instances. */
  private buildHostRole(): iam.Role {
    const role = new iam.Role(this.stack, "ecs-host-role", {
      assumedBy: new iam.ServicePrincipal("ec2.amazonaws.com"),
      roleName: this.buildResourceName("ecs-host-role", true),
    });
    role.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AmazonEC2ContainerServiceforEC2Role"),
    );
    for (const policyArn of this.getEc2InstanceManagedPolicies()) {
      role.addManagedPolicy(iam.ManagedPolicy.fromManagedPolicyArn(this.stack, `ecs-host-policy-${policyArn}`, policyArn));
    }
    return role;
  }

  /**
   * The configured host family. Its architecture selects the host image and the processor
   * architecture of every task definition, so the two cannot disagree by configuration. A family
   * this code cannot resolve is refused here: the alternative is a launch template with an image for
   * the wrong architecture, whose hosts boot and never register, and tasks that are never placed.
   *
   * This assumes `ecs.image` is a manifest with both architectures. A single-architecture image on
   * the other family is a task that pulls and fails to run, which the stack cannot see at synthesis.
   */
  private hostInstanceType(): ec2.InstanceType {
    const configured = this.requiredString("ecs.hosts.instance_type");
    const instanceType = new ec2.InstanceType(configured);
    try {
      instanceType.architecture;
    } catch (cause) {
      throw new Error(
        `ecs.hosts.instance_type ${configured} is not an instance type whose architecture this stack can resolve. Set a family with a size, for example m7g.large.`,
        { cause },
      );
    }
    return instanceType;
  }

  /**
   * Makes the host group deletable by the platform on its own.
   *
   * This depends on the group, so CloudFormation creates it straight after the group and deletes
   * it straight before, which is the ordering that matters: on a rollback nothing of ours is
   * running, and this is the last thing to execute while the group still exists. Without it a
   * container stack that fails for any reason cannot roll back, because the group cannot remove
   * instances that are protected from scale-in and no code clears the flag.
   */
  private releaseScaleInProtectionOnDelete(): void {
    const onEvent = new lambda.Function(this.stack, "host-scale-in-release-fn", {
      code: lambda.Code.fromInline(RELEASE_SCALE_IN_PROTECTION_HANDLER),
      handler: "index.handler",
      runtime: lambda.Runtime.PYTHON_3_13,
      timeout: Duration.minutes(5),
    });
    onEvent.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["autoscaling:DescribeAutoScalingGroups"],
        resources: ["*"],
      }),
    );
    onEvent.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["autoscaling:SetInstanceProtection", "autoscaling:UpdateAutoScalingGroup"],
        resources: [
          `arn:${Aws.PARTITION}:autoscaling:${Aws.REGION}:${Aws.ACCOUNT_ID}:autoScalingGroup:*:autoScalingGroupName/${this.hostAutoScalingGroup.autoScalingGroupName}`,
        ],
      }),
    );
    this.scaleInRelease = new CustomResource(this.stack, "host-scale-in-release", {
      properties: { AutoScalingGroupName: this.hostAutoScalingGroup.autoScalingGroupName },
      resourceType: "Custom::ReleaseScaleInProtection",
      serviceToken: new Provider(this.stack, "host-scale-in-release-provider", {
        onEventHandler: onEvent,
      }).serviceToken,
    });
    this.scaleInRelease.node.addDependency(this.hostAutoScalingGroup);
  }

  /** Builds the ECS host group and its metadata-isolating launch template. */
  private buildHostAutoScalingGroup(): autoscaling.AutoScalingGroup {
    const userData = ec2.UserData.forLinux();
    userData.addCommands(
      "mkdir -p /etc/ecs",
      `echo ECS_CLUSTER=${this.ecsCluster.clusterName} >> /etc/ecs/ecs.config`,
      "echo ECS_AWSVPC_BLOCK_IMDS=true >> /etc/ecs/ecs.config",
      "install -d -o root -g root -m 0755 /var/run/datadog",
      ...this.hostStorageCommands(),
    );
    const launchTemplate = new ec2.LaunchTemplate(this.stack, "ecs-host-launch-template", {
      blockDevices: [
        {
          deviceName: "/dev/xvda",
          volume: ec2.BlockDeviceVolume.ebs(this.context.config.getInt("ecs.hosts.volume_size", 60), {
            encrypted: true,
            volumeType: ec2.EbsDeviceVolumeType.GP3,
          }),
        },
      ],
      instanceType: this.hostInstanceType(),
      machineImage: ec2.MachineImage.resolveSsmParameterAtLaunch(hostImageParameter(this.hostArchitecture)),
      httpPutResponseHopLimit: 1,
      requireImdsv2: true,
      role: this.hostRole,
      securityGroup: this.hostSecurityGroup,
      userData,
    });
    const autoScalingGroup = new autoscaling.AutoScalingGroup(this.stack, "ecs-host-auto-scaling-group", {
      autoScalingGroupName: this.buildResourceName("ecs-hosts"),
      launchTemplate,
      maxCapacity: this.context.config.getInt("ecs.hosts.max", 4),
      minCapacity: this.context.config.getInt("ecs.hosts.min", 3),
      newInstancesProtectedFromScaleIn: true,
      vpc: this.cluster.vpc,
      vpcSubnets: { subnets: this.cluster.privateSubnets },
    });
    this.addCommonTags(autoScalingGroup);
    return autoScalingGroup;
  }

  /**
   * Mount commands are emitted only for ONTAP and Lustre, whose host-mounted
   * paths are subsequently bind-mounted into the tasks.
   */
  private hostStorageCommands(): string[] {
    const commands: string[] = [];
    for (const mount of this.storageMounts()) {
      if (mount.hostPath === undefined) continue;
      commands.push(`mkdir -p ${mount.hostPath}`);
      const storage = this.context.config.getConfig(`shared-storage.${mount.name}`, {});
      if (storage === undefined) continue;
      const provider = storage["provider"];
      if (provider === "fsx_lustre") {
        const lustre = storage["fsx_lustre"];
        if (isRecord(lustre) && typeof lustre["dns"] === "string" && typeof lustre["mount_name"] === "string") {
          commands.push(
            `mount -t lustre ${lustre["dns"]}@tcp:/${lustre["mount_name"]} ${mount.hostPath}`,
          );
        }
      }
      if (provider === "fsx_netapp_ontap") {
        const ontap = storage["fsx_netapp_ontap"];
        const svm = isRecord(ontap) ? ontap["svm"] : undefined;
        if (isRecord(svm) && typeof svm["nfs_dns"] === "string") {
          commands.push(`mount -t nfs ${svm["nfs_dns"]} ${mount.hostPath}`);
        }
      }
    }
    return commands;
  }

  /** Applies the physical-name rule used by application role constructs. */
  private applicationRoleName(name: string): string {
    const fullName = this.buildResourceName(name, true);
    return fullName.length <= 64
      ? fullName
      : buildTrimmedResourceName(this.clusterName, name, this.awsRegion, 64);
  }

  /** An IAM role ARN built from a name this account applies the same rule to. */
  private roleArnByName(name: string): string {
    return Fn.join("", ["arn:", Aws.PARTITION, ":iam::", Aws.ACCOUNT_ID, ":role/", this.applicationRoleName(name)]);
  }

  /**
   * The managed policies the module instance roles carry. A task role gets the same set, so the
   * permissions a task holds do not change when it stops borrowing the host role.
   */
  private taskManagedPolicies(): iam.IManagedPolicy[] {
    if (this.instanceManagedPolicies !== undefined) return this.instanceManagedPolicies;
    this.instanceManagedPolicies = this.getEc2InstanceManagedPolicies().map((policyArn, index) =>
      iam.ManagedPolicy.fromManagedPolicyArn(this.stack, `ecs-task-managed-policy-${index}`, policyArn),
    );
    return this.instanceManagedPolicies;
  }

  /**
   * The task identity for one role.
   *
   * Each role gets its own, so one task cannot use another's permissions, and the policy renders
   * from the template the module's instance role renders from rather than being written again by
   * hand. The trust is the account-scoped task principal.
   */
  private taskRole(role: ContainerRole): iam.Role {
    const existing = this.taskRoles[role];
    if (existing !== undefined) return existing;

    const taskRole = new iam.Role(this.stack, `${role}-task-role`, {
      assumedBy: this.ecsTasksPrincipal(),
      description: `IAM role assigned to the ${role} ECS task`,
      managedPolicies: this.taskManagedPolicies(),
      roleName: this.applicationRoleName(`ecs-${role}-task-role`),
    });
    taskRole.attachInlinePolicy(this.buildTaskPolicy(role, taskRole));
    this.addCommonTags(taskRole);
    this.taskRoles[role] = taskRole;
    return taskRole;
  }

  /**
   * The inline policy for one task role.
   *
   * The scheduler template names the roles it may pass to a compute node or a spot fleet request.
   * Those roles belong to the scheduler stack, which deploys after this one, so their ARNs come from
   * the naming rule the account applies rather than from a setting that does not exist yet.
   */
  private buildTaskPolicy(role: ContainerRole, taskRole: iam.Role): Policy {
    const schedulerModuleId = this.context.config.moduleId("scheduler");
    const vars: Record<string, unknown> = role === "scheduler"
      ? {
          compute_node_role_arn: this.roleArnByName(`${schedulerModuleId}-compute-node-role`),
          scheduler_role_arn: taskRole.roleArn,
          spot_fleet_request_role_arn: this.roleArnByName(`${schedulerModuleId}-spot-fleet-request-role`),
        }
      : { role_arn: taskRole.roleArn };
    return new Policy(this.context, `ecs-${role}-task-policy`, this.stack, {
      moduleId: this.ideaModuleId(role),
      policyTemplateName: TASK_POLICY_TEMPLATES[role],
      vars,
    });
  }

  /**
   * Uses operator-provided certificate inputs when present. Otherwise this
   * stack creates the self-signed gateway secrets before the gateway task.
   */
  private loadGatewayCertificateSecrets(): {
    certificateSecretArn: string;
    privateKeySecretArn: string;
  } {
    const certificatePrefix = "virtual-desktop-controller.dcv_connection_gateway.certificate";
    const certificateSecretArn = this.context.config.getString(`${certificatePrefix}.certificate_secret_arn`);
    const privateKeySecretArn = this.context.config.getString(`${certificatePrefix}.private_key_secret_arn`);
    if (this.context.config.getBool(`${certificatePrefix}.provided`, false)) {
      return {
        certificateSecretArn: this.requiredString(`${certificatePrefix}.certificate_secret_arn`),
        privateKeySecretArn: this.requiredString(`${certificatePrefix}.private_key_secret_arn`),
      };
    }
    if (
      certificateSecretArn !== undefined &&
      certificateSecretArn !== "" &&
      privateKeySecretArn !== undefined &&
      privateKeySecretArn !== ""
    ) {
      return { certificateSecretArn, privateKeySecretArn };
    }

    const vdcModuleId = this.context.config.moduleId("virtual-desktop-controller");
    const certificateName = `${this.clusterName}-${vdcModuleId}-gateway-certificate`;
    const properties: Record<string, unknown> = {
      certificate_name: certificateName,
      create_acm_certificate: false,
      domain_name: `${vdcModuleId}.${this.clusterName}.idea.default`,
      tags: {
        Name: `${this.clusterName}-${vdcModuleId}-gateway Self Signed Certificate`,
        "idea:ClusterName": this.clusterName,
        "idea:ModuleName": "virtual-desktop-controller",
      },
    };
    const kmsKeyId = this.context.config.getString("cluster.secretsmanager.kms_key_id");
    if (kmsKeyId !== undefined && kmsKeyId !== "") properties["kms_key_id"] = kmsKeyId;

    const certificate = new CustomResource(this.stack, "gateway-self-signed-certificate", {
      properties,
      resourceType: "Custom::SelfSignedCertificateConnectionGateway",
      serviceToken: this.requiredString("cluster.self_signed_certificate_lambda_arn"),
    });
    return {
      certificateSecretArn: certificate.getAttString("certificate_secret_arn"),
      privateKeySecretArn: certificate.getAttString("private_key_secret_arn"),
    };
  }

  /** Builds a secret-free execution role with an account-scoped trust policy. */
  /** The task service, trusted only from this account. */
  private ecsTasksPrincipal(): iam.ServicePrincipal {
    return new iam.ServicePrincipal("ecs-tasks.amazonaws.com", {
      conditions: {
        StringEquals: { "aws:SourceAccount": this.stack.account },
      },
    });
  }

  private buildExecutionRole(constructId: string): iam.Role {
    const role = new iam.Role(this.stack, constructId, {
      assumedBy: this.ecsTasksPrincipal(),
      roleName: this.buildResourceName(constructId, true),
    });
    role.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AmazonECSTaskExecutionRolePolicy"));
    return role;
  }

  /** Creates the five awsvpc application task definitions and services. */
  private buildApplicationServices(): void {
    this.roleResources["cluster-manager"] = this.buildApiService("cluster-manager", {
      component: undefined,
      targetGroups: [
        this.applicationTargetGroup("cm-ecs-e", 8443, "/healthcheck"),
        this.applicationTargetGroup("cm-ecs-i", 8443, "/healthcheck"),
        this.applicationTargetGroup("cm-ecs-w", 8443, "/healthcheck"),
      ],
    });
    this.roleResources.vdc = this.buildApiService("vdc", {
      component: "controller",
      targetGroups: [
        this.applicationTargetGroup("vdc-ecs-e", 8443, "/healthcheck"),
        this.applicationTargetGroup("vdc-ecs-i", 8443, "/healthcheck"),
      ],
    });
    this.roleResources.scheduler = this.buildSchedulerService();
    this.roleResources["dcv-broker"] = this.buildBrokerService();
    this.roleResources["dcv-gateway"] = this.buildGatewayService();
  }

  /**
   * Attaches every target group this stack creates to the listener that serves it.
   *
   * A service may not name a target group that has no load balancer, and this module deploys
   * before the modules whose stacks create those listener rules today, so without this the
   * services are refused and the stack rolls back. The listeners themselves are cluster-stack
   * resources and exist well before this module runs.
   *
   * The endpoint names are the module stacks' own, deliberately. The handler keys a rule by its
   * `idea:EndpointName` tag and adopts one that is already there, so the module stack's later
   * resource converges on this same rule rather than making a second, and on a cluster that
   * already has the rule this modifies it where it stands, preserving its identity and priority.
   */
  private buildEndpoints(): void {
    const config = this.context.config;
    const serviceToken = this.requiredString("cluster.cluster_endpoints_lambda_arn");
    const externalListener = this.requiredString(
      "cluster.load_balancers.external_alb.https_listener_arn",
    );
    const internalListener = this.requiredString(
      "cluster.load_balancers.internal_alb.https_listener_arn",
    );
    const clusterManagerId = config.moduleId("cluster-manager");
    const schedulerId = config.moduleId("scheduler");
    const vdcId = config.moduleId("virtual-desktop-controller");

    /**
     * The service may not be created before the endpoint that gives its target group a load
     * balancer. Both only reference the target group, so without this they are siblings and
     * CloudFormation creates them in parallel: the service wins the race and is refused, which is
     * the failure this whole method exists to prevent.
     */
    const attachBefore = (role: ContainerRole, endpoint: CustomResource): void => {
      this.roleResources[role]?.service.node.addDependency(endpoint);
    };

    /** One routed endpoint: a rule at the module's own priority and path patterns. */
    const rule = (
      role: ContainerRole,
      constructId: string,
      endpointName: string,
      listenerArn: string,
      prefix: string,
      targetGroup: elbv2.IApplicationTargetGroup,
    ): void => {
      const endpoint = new CustomResource(this.stack, constructId, {
        properties: {
          endpoint_name: endpointName,
          listener_arn: listenerArn,
          priority: config.getInt(`${prefix}.priority`, undefined, { required: true }),
          conditions: [
            {
              Field: "path-pattern",
              Values: config.getList<string>(`${prefix}.path_patterns`, [], { required: true }),
            },
          ],
          actions: [{ Type: "forward", TargetGroupArn: targetGroup.targetGroupArn }],
        },
        resourceType: "Custom::EcsEndpoint",
        serviceToken,
      });
      attachBefore(role, endpoint);
    };

    /** One listener whose default action is this target group. No rule, so no priority. */
    const defaultAction = (
      role: ContainerRole,
      constructId: string,
      endpointName: string,
      listenerArn: string,
      targetGroup: elbv2.IApplicationTargetGroup,
    ): void => {
      const endpoint = new CustomResource(this.stack, constructId, {
        properties: {
          endpoint_name: endpointName,
          listener_arn: listenerArn,
          priority: 0,
          default_action: true,
          actions: [{ Type: "forward", TargetGroupArn: targetGroup.targetGroupArn }],
        },
        resourceType: "Custom::EcsDefaultEndpoint",
        serviceToken,
      });
      attachBefore(role, endpoint);
    };

    const groups = (role: ContainerRole): elbv2.IApplicationTargetGroup[] =>
      this.roleResources[role]?.targetGroups as elbv2.IApplicationTargetGroup[];

    const clusterManager = groups("cluster-manager");
    rule("cluster-manager", "cm-external-endpoint", `${clusterManagerId}-external-endpoint`, externalListener, "cluster-manager.endpoints.external", clusterManager[0]);
    rule("cluster-manager", "cm-internal-endpoint", `${clusterManagerId}-internal-endpoint`, internalListener, "cluster-manager.endpoints.internal", clusterManager[1]);
    defaultAction("cluster-manager", "cm-web-portal-endpoint", `${clusterManagerId}-web-portal-endpoint`, externalListener, clusterManager[2]);

    const scheduler = groups("scheduler");
    rule("scheduler", "scheduler-external-endpoint", `${schedulerId}-external-endpoint`, externalListener, "scheduler.endpoints.external", scheduler[0]);
    rule("scheduler", "scheduler-internal-endpoint", `${schedulerId}-internal-endpoint`, internalListener, "scheduler.endpoints.internal", scheduler[1]);

    const vdc = groups("vdc");
    rule("vdc", "vdc-external-endpoint", `${vdcId}-controller-endpoint-ext`, externalListener, "virtual-desktop-controller.controller.endpoints.external", vdc[0]);
    rule("vdc", "vdc-internal-endpoint", `${vdcId}-controller-endpoint-int`, internalListener, "virtual-desktop-controller.controller.endpoints.internal", vdc[1]);

    // The broker's three listeners each forward everything to one target group, so each is a
    // default action. The agent endpoint registers under the client endpoint's name, which is how
    // it is deployed today and is the name the module stack uses.
    const broker = groups("dcv-broker");
    defaultAction("dcv-broker", "broker-client-endpoint", "broker-client-endpoint", this.requiredString("cluster.load_balancers.internal_alb.dcv_broker_client_listener_arn"), broker[0]);
    defaultAction("dcv-broker", "broker-agent-endpoint", "broker-client-endpoint", this.requiredString("cluster.load_balancers.internal_alb.dcv_broker_agent_listener_arn"), broker[1]);
    defaultAction("dcv-broker", "broker-gateway-endpoint", "broker-gateway-endpoint", this.requiredString("cluster.load_balancers.internal_alb.dcv_broker_gateway_listener_arn"), broker[2]);
  }

  /** Creates a cluster-manager or VDC service with HTTPS API target groups. */
  private buildApiService(
    role: "cluster-manager" | "vdc",
    input: { component: string | undefined; targetGroups: elbv2.ApplicationTargetGroup[] },
  ): RoleResources {
    const taskDefinition = this.buildTaskDefinition(role);
    const logGroupName = this.agentLogGroupName(role, input.component);
    const container = taskDefinition.addContainer(`${role}-container`, {
      cpu: this.roleSizing(role).cpu,
      dockerLabels: this.dockerLabels(role),
      environment: this.commonEnvironment(role),
      image: ecs.ContainerImage.fromRegistry(this.requiredString("ecs.image")),
      logging: this.adoptedLogDriver(`${role}-logs`, logGroupName, STREAM_PREFIX_APPLICATION),
      memoryLimitMiB: this.roleSizing(role).memory,
    });
    container.addPortMappings({ containerPort: 8443, protocol: ecs.Protocol.TCP });
    this.bindContainerToLogGroup(container, logGroupName);
    this.addStorageMounts(taskDefinition, container);
    this.attachApplicationFileLogs(role, taskDefinition, container);
    const service = this.buildEc2Service(role, taskDefinition, this.roleSizing(role).desired, {
      minHealthyPercent: 50,
      maxHealthyPercent: 200,
    });
    for (const targetGroup of input.targetGroups) service.attachToApplicationTargetGroup(targetGroup);
    return { service, targetGroups: input.targetGroups };
  }

  /** Creates the scheduler service and its deep container health check. */
  private buildSchedulerService(): RoleResources {
    const role: ContainerRole = "scheduler";
    const taskDefinition = this.buildTaskDefinition(role);
    const logGroupName = this.agentLogGroupName(role);
    const openPbsLogGroupName = `${logGroupName}/openpbs`;
    const container = taskDefinition.addContainer("scheduler-container", {
      cpu: this.roleSizing(role).cpu,
      dockerLabels: this.dockerLabels(role),
      environment: {
        ...this.commonEnvironment(role),
        IDEA_ROUTE53_ZONE_ID: this.requiredString("cluster.route53.private_hosted_zone_id"),
        IDEA_SCHEDULER_DNS_NAME: `scheduler.${this.clusterName}.${this.awsRegion}.local`,
        PBS_HOME: SCHEDULER_PBS_HOME,
        PBS_NODE_FAIL_REQUEUE: "600",
      },
      healthCheck: {
        command: [
          "CMD-SHELL",
          "qstat -B && curl --fail --silent --show-error --unix-socket /run/idea.sock --max-time 4 --header 'Content-Type: application/json' --data '{\"header\":{\"namespace\":\"Scheduler.ListActiveJobs\"}}' http://localhost/scheduler/api/v1",
        ],
        interval: Duration.seconds(30),
        retries: 3,
        // The container check has no allowance but this one, on a first start and on every
        // replacement, so it matches the load balancer grace. A shorter period would let the
        // platform kill the container while `roles/scheduler.sh` is still inside its own wait for
        // the batch server.
        startPeriod: this.taskStartAllowance("scheduler"),
      },
      image: ecs.ContainerImage.fromRegistry(this.requiredString("ecs.image")),
      logging: this.adoptedLogDriver("scheduler-logs", logGroupName, STREAM_PREFIX_APPLICATION),
      memoryLimitMiB: this.roleSizing(role).memory,
    });
    container.addPortMappings({ containerPort: 8443, protocol: ecs.Protocol.TCP });
    this.bindContainerToLogGroup(container, logGroupName);
    this.addSchedulerStorage(taskDefinition, container);
    this.addStorageMounts(taskDefinition, container);
    this.attachApplicationFileLogs(role, taskDefinition, container);
    this.addLogTailContainer(taskDefinition, {
      constructId: "scheduler-openpbs-logs",
      containerId: "scheduler-openpbs-logs",
      directories: [
        `${SCHEDULER_PBS_HOME}/server_logs`,
        `${SCHEDULER_PBS_HOME}/sched_logs`,
        `${SCHEDULER_PBS_HOME}/server_priv/accounting`,
      ],
      logGroupName: openPbsLogGroupName,
      streamPrefix: STREAM_PREFIX_OPENPBS,
      sourceVolume: "scheduler-pbs",
      containerPath: SCHEDULER_PBS_HOME,
      readOnly: true,
    });
    const targetGroups = [
      this.applicationTargetGroup("sched-ecs-e", 8443, "/healthcheck", 15),
      this.applicationTargetGroup("sched-ecs-i", 8443, "/healthcheck", 15),
    ];
    // The batch server holds a single-writer lock on its state directory, so two scheduler tasks
    // cannot run at once: the second to start fails to take the lock. A maximum of one hundred
    // leaves no room for a replacement to start before the running task stops, which is why this
    // one service differs from the other four. The cost is that a replacement is a short batch
    // server outage rather than a handover: running jobs survive, submissions fail while it is down.
    const service = this.buildEc2Service(role, taskDefinition, this.roleSizing(role).desired, {
      minHealthyPercent: 0,
      maxHealthyPercent: 100,
    });
    for (const targetGroup of targetGroups) service.attachToApplicationTargetGroup(targetGroup);
    return { service, targetGroups };
  }

  /** Creates the service-discoverable DCV broker and its three listeners. */
  private buildBrokerService(): RoleResources {
    const role: ContainerRole = "dcv-broker";
    const taskDefinition = this.buildTaskDefinition(role);
    const logGroupName = this.agentLogGroupName(role, "dcv-broker");
    const container = taskDefinition.addContainer("dcv-broker-container", {
      cpu: this.roleSizing(role).cpu,
      dockerLabels: this.dockerLabels(role),
      environment: {
        ...this.commonEnvironment(role),
        IDEA_COGNITO_PROVIDER_URL: this.requiredString("identity-provider.cognito.provider_url"),
        IDEA_SERVICE_DISCOVERY_NAME: `vdc-broker.${this.clusterName}.ecs.local`,
        // The task network namespace has no second address family, so a dual-stack JVM fails to
        // create its sockets. The virtual machine reads this variable itself at startup, which is
        // why it is this name and not one the vendor launcher would have to pass on.
        JAVA_TOOL_OPTIONS: "-Djava.net.preferIPv4Stack=true",
      },
      image: ecs.ContainerImage.fromRegistry(this.requiredString("ecs.image")),
      logging: this.adoptedLogDriver("dcv-broker-logs", logGroupName, STREAM_PREFIX_BROKER),
      memoryLimitMiB: this.roleSizing(role).memory,
    });
    for (const port of [8444, 8445, 8446]) {
      container.addPortMappings({ containerPort: port, protocol: ecs.Protocol.TCP });
    }
    taskDefinition.addVolume({ name: "broker-logs" });
    container.addMountPoints({
      containerPath: BROKER_LOG_DIRECTORY,
      readOnly: false,
      sourceVolume: "broker-logs",
    });
    this.bindContainerToLogGroup(container, logGroupName);
    this.addStorageMounts(taskDefinition, container);
    this.addLogTailContainer(taskDefinition, {
      constructId: "dcv-broker-file-logs",
      containerId: "dcv-broker-file-logs",
      directories: [BROKER_LOG_DIRECTORY],
      logGroupName,
      streamPrefix: STREAM_PREFIX_BROKER,
      sourceVolume: "broker-logs",
      containerPath: BROKER_LOG_DIRECTORY,
      readOnly: true,
    });
    const targetGroups = [
      this.applicationTargetGroup("brk-ecs-c", 8444, "/health"),
      this.applicationTargetGroup("brk-ecs-a", 8445, "/health"),
      this.applicationTargetGroup("brk-ecs-g", 8446, "/health"),
    ];
    const service = this.buildEc2Service(role, taskDefinition, this.roleSizing(role).desired, {
      cloudMapOptions: {
        cloudMapNamespace: this.namespace,
        dnsRecordType: servicediscovery.DnsRecordType.A,
        dnsTtl: Duration.seconds(10),
        failureThreshold: 1,
        name: "vdc-broker",
      },
      minHealthyPercent: 50,
      maxHealthyPercent: 200,
    });
    for (const targetGroup of targetGroups) service.attachToApplicationTargetGroup(targetGroup);
    return { service, targetGroups };
  }

  /** Creates the DCV gateway service and target groups for TCP and TCP/UDP listeners. */
  private buildGatewayService(): RoleResources {
    const role: ContainerRole = "dcv-gateway";
    const taskDefinition = this.buildTaskDefinition(role, this.gatewayExecutionRole);
    const logGroupName = this.agentLogGroupName(role, "dcv-connection-gateway");
    const certificate = secretsmanager.Secret.fromSecretCompleteArn(
      this.stack,
      "gateway-certificate-secret",
      this.gatewayCertificateSecretArn,
    );
    const privateKey = secretsmanager.Secret.fromSecretCompleteArn(
      this.stack,
      "gateway-private-key-secret",
      this.gatewayPrivateKeySecretArn,
    );
    const container = taskDefinition.addContainer("dcv-gateway-container", {
      cpu: this.roleSizing(role).cpu,
      dockerLabels: this.dockerLabels(role),
      environment: {
        ...this.commonEnvironment(role),
        IDEA_INTERNAL_ALB_ENDPOINT: `https://${this.requiredString("cluster.load_balancers.internal_alb.load_balancer_dns_name")}`,
      },
      image: ecs.ContainerImage.fromRegistry(this.requiredString("ecs.image")),
      logging: this.adoptedLogDriver("dcv-gateway-logs", logGroupName, STREAM_PREFIX_GATEWAY),
      memoryLimitMiB: this.roleSizing(role).memory,
      secrets: {
        DCV_GATEWAY_CERT_PEM: ecs.Secret.fromSecretsManager(certificate),
        DCV_GATEWAY_KEY_PEM: ecs.Secret.fromSecretsManager(privateKey),
      },
    });
    // The gateway serves both stream protocols on 8443, but a container port may appear in only
    // one mapping: the service API refuses a second. Under `awsvpc` the task owns its network
    // interface and the mapping does not filter traffic, so one mapping publishes the port and the
    // task security group is what admits each protocol. The health port 8989 is separate and
    // unaffected.
    container.addPortMappings({ containerPort: 8443, protocol: ecs.Protocol.TCP });
    taskDefinition.addVolume({ name: "gateway-logs" });
    container.addMountPoints({
      containerPath: GATEWAY_LOG_DIRECTORY,
      readOnly: false,
      sourceVolume: "gateway-logs",
    });
    this.bindContainerToLogGroup(container, logGroupName);
    this.addStorageMounts(taskDefinition, container);
    this.addLogTailContainer(taskDefinition, {
      constructId: "dcv-gateway-file-logs",
      containerId: "dcv-gateway-file-logs",
      directories: [GATEWAY_LOG_DIRECTORY],
      logGroupName,
      streamPrefix: STREAM_PREFIX_GATEWAY,
      sourceVolume: "gateway-logs",
      containerPath: GATEWAY_LOG_DIRECTORY,
      readOnly: true,
    });
    // The desktop stack attaches exactly one of these to its network load balancer, chosen by the
    // same setting. Creating both leaves the other with no load balancer for ever, and a service
    // may not name a target group that has none, so the service could never be created.
    const quicSupported = this.context.config.getBool(
      "virtual-desktop-controller.dcv_session.quic_support",
      false,
    );
    const targetGroups = [
      quicSupported
        ? this.networkTargetGroup("gw-ecs-TUN", elbv2.Protocol.TCP_UDP)
        : this.networkTargetGroup("gw-ecs-TN", elbv2.Protocol.TCP),
    ];
    const service = this.buildEc2Service(role, taskDefinition, this.roleSizing(role).desired, {
      minHealthyPercent: 50,
      maxHealthyPercent: 200,
    });
    for (const targetGroup of targetGroups) service.attachToNetworkTargetGroup(targetGroup);
    return { service, targetGroups };
  }

  /** Creates a task definition with isolated roles and retained revisions. */
  private buildTaskDefinition(
    role: ContainerRole,
    executionRole: iam.IRole = this.executionRole,
  ): ecs.Ec2TaskDefinition {
    const taskDefinition = new ecs.Ec2TaskDefinition(this.stack, `${role}-task-definition`, {
      executionRole,
      networkMode: ecs.NetworkMode.AWS_VPC,
      taskRole: this.taskRole(role),
    });
    const resource = taskDefinition.node.defaultChild;
    if (resource instanceof ecs.CfnTaskDefinition) {
      resource.runtimePlatform = {
        cpuArchitecture: cpuArchitecture(this.hostArchitecture),
        operatingSystemFamily: "LINUX",
      };
    }
    taskDefinition.applyRemovalPolicy(RemovalPolicy.RETAIN);
    return taskDefinition;
  }

  /**
   * How long this role's container takes to answer a health request.
   *
   * A role waits only for what its own start does: the batch server wait for the scheduler, the
   * service discovery wait for the broker, and the application allowance for every role.
   */
  private taskStartAllowance(role: ContainerRole): Duration {
    if (role === "scheduler") return Duration.seconds(PBS_SERVER_WAIT_SECONDS + APPLICATION_START_SECONDS);
    if (role === "dcv-broker") return Duration.seconds(BROKER_DISCOVERY_WAIT_SECONDS + APPLICATION_START_SECONDS);
    return Duration.seconds(APPLICATION_START_SECONDS);
  }

  /** The task start plus the time its target needs to register as healthy behind a load balancer. */
  private healthCheckGrace(role: ContainerRole): Duration {
    return Duration.seconds(this.taskStartAllowance(role).toSeconds() + TARGET_REGISTRATION_SECONDS);
  }

  /** Creates an EC2 service using the shared capacity provider. */
  private buildEc2Service(
    role: ContainerRole,
    taskDefinition: ecs.Ec2TaskDefinition,
    desiredCount: number,
    input: {
      cloudMapOptions?: ecs.CloudMapOptions;
      minHealthyPercent: number;
      maxHealthyPercent: number;
    },
  ): ecs.Ec2Service {
    const service = new ecs.Ec2Service(this.stack, `${role}-service`, {
      capacityProviderStrategies: [{ capacityProvider: this.capacityProvider.capacityProviderName, weight: 1 }],
      circuitBreaker: { rollback: true },
      cloudMapOptions: input.cloudMapOptions,
      cluster: this.ecsCluster,
      desiredCount,
      // An operator's diagnosis path into a running control-plane task, per task and per session
      // and logged to the cluster's exec group. The permission it needs is already on every task
      // role through the instance managed policies, so this uses a standing grant rather than
      // adding one. It stays out of this tool's own identity: the tool observes and refuses.
      enableExecuteCommand: true,
      healthCheckGracePeriod: this.healthCheckGrace(role),
      maxHealthyPercent: input.maxHealthyPercent,
      minHealthyPercent: input.minHealthyPercent,
      placementConstraints: role === "scheduler" ? undefined : [ecs.PlacementConstraint.distinctInstances()],
      securityGroups: [this.taskSecurityGroup(role)],
      taskDefinition,
      vpcSubnets: { subnets: this.cluster.privateSubnets },
    });
    // A service is the likeliest thing in this stack to fail, and the release has to already
    // exist when it does. Depending on the host group alone is not enough: the two are created in
    // parallel, and a failing service cancels the release before it finishes, so the rollback has
    // nothing to clear the protection with.
    service.node.addDependency(this.scaleInRelease);
    return service;
  }

  /** Creates an HTTPS application target group for an awsvpc service. */
  private applicationTargetGroup(
    identifier: string,
    port: number,
    healthCheckPath: string,
    deregistrationDelaySeconds?: number,
  ): elbv2.ApplicationTargetGroup {
    return new elbv2.ApplicationTargetGroup(this.stack, `${identifier}-target-group`, {
      deregistrationDelay:
        deregistrationDelaySeconds === undefined ? undefined : Duration.seconds(deregistrationDelaySeconds),
      healthCheck: { path: healthCheckPath, protocol: elbv2.Protocol.HTTPS },
      port,
      protocol: elbv2.ApplicationProtocol.HTTPS,
      targetGroupName: this.getTargetGroupName(identifier),
      targetType: elbv2.TargetType.IP,
      vpc: this.cluster.vpc,
    });
  }

  /** Creates an IP target group for the existing network load balancer. */
  private networkTargetGroup(identifier: string, protocol: elbv2.Protocol): elbv2.NetworkTargetGroup {
    const targetGroup = new elbv2.NetworkTargetGroup(this.stack, `${identifier}-target-group`, {
      connectionTermination: true,
      healthCheck: {
        port: "8989",
        protocol: elbv2.Protocol.TCP,
      },
      port: 8443,
      protocol,
      targetGroupName: this.getTargetGroupName(identifier),
      targetType: elbv2.TargetType.IP,
      vpc: this.cluster.vpc,
    });
    targetGroup.setAttribute("stickiness.enabled", "true");
    targetGroup.setAttribute("stickiness.type", "source_ip");
    return targetGroup;
  }

  /** Returns the configured CPU, memory and desired count for a role. */
  private roleSizing(role: ContainerRole): RoleSizing {
    return {
      cpu: this.requiredInt(`ecs.tasks.${role}.cpu`),
      memory: this.requiredInt(`ecs.tasks.${role}.memory`),
      desired: this.requiredInt(`ecs.tasks.${role}.desired`),
    };
  }

  /** Common task environment values. */
  private commonEnvironment(role: ContainerRole): Record<string, string> {
    return {
      AWS_DEFAULT_REGION: this.awsRegion,
      DD_DOGSTATSD_URL: "unix:///var/run/datadog/dsd.socket",
      IDEA_CLUSTER_NAME: this.clusterName,
      IDEA_CONTAINER_ROLE: role,
      IDEA_MODULE_ID: this.ideaModuleId(role),
      IDEA_MODULE_NAME: this.ideaModuleName(role),
      IDEA_MODULE_SET: this.context.config.moduleSet,
    };
  }

  /** Labels consumed by the observability agent. */
  private dockerLabels(role: ContainerRole): Record<string, string> {
    return {
      "com.datadoghq.tags.env": this.clusterName,
      "com.datadoghq.tags.service": role,
    };
  }

  /** Returns EFS and host bind mounts declared by shared-storage settings. */
  private storageMounts(): StorageMount[] {
    const mounts: StorageMount[] = [];
    const storageRoot = this.context.config.getConfig("shared-storage", {}) ?? {};
    for (const [name, storage] of Object.entries(storageRoot)) {
      if (!isRecord(storage) || typeof storage["mount_dir"] !== "string" || typeof storage["provider"] !== "string") {
        continue;
      }
      const mountPath = storage["mount_dir"];
      if (storage["provider"] === "efs") {
        const efs = storage["efs"];
        if (isRecord(efs) && typeof efs["file_system_id"] === "string") {
          mounts.push({ fileSystemId: efs["file_system_id"], mountPath, name });
        }
      }
      if (storage["provider"] === "fsx_lustre" || storage["provider"] === "fsx_netapp_ontap") {
        mounts.push({ hostPath: mountPath, mountPath, name });
      }
    }
    return mounts;
  }

  /** Adds storage and the optional DogStatsD host volume to an app task. */
  private addStorageMounts(taskDefinition: ecs.Ec2TaskDefinition, container: ecs.ContainerDefinition): void {
    for (const mount of this.storageMounts()) {
      const volumeName = `storage-${mount.name}`;
      if (mount.fileSystemId !== undefined) {
        taskDefinition.addVolume({
          efsVolumeConfiguration: { fileSystemId: mount.fileSystemId },
          name: volumeName,
        });
      } else if (mount.hostPath !== undefined) {
        taskDefinition.addVolume({ host: { sourcePath: mount.hostPath }, name: volumeName });
      } else {
        continue;
      }
      container.addMountPoints({ containerPath: mount.mountPath, readOnly: false, sourceVolume: volumeName });
    }
    if (this.context.config.getBool("ecs.datadog.enabled", false)) {
      taskDefinition.addVolume({ host: { sourcePath: "/var/run/datadog" }, name: "datadog" });
      container.addMountPoints({
        containerPath: "/var/run/datadog",
        readOnly: true,
        sourceVolume: "datadog",
      });
    }
  }

  /** Adds a scheduler-only EFS file system for persistent PBS state. */
  private addSchedulerStorage(
    taskDefinition: ecs.Ec2TaskDefinition,
    container: ecs.ContainerDefinition,
  ): void {
    const schedulerSecurityGroup = this.taskSecurityGroup("scheduler");
    const fileSystemSecurityGroup = new ec2.SecurityGroup(
      this.stack,
      "scheduler-pbs-file-system-security-group",
      {
        allowAllOutbound: false,
        description: "Allows NFS only from the scheduler task and its host",
        vpc: this.cluster.vpc,
      },
    );
    fileSystemSecurityGroup.addIngressRule(
      schedulerSecurityGroup,
      ec2.Port.tcp(2049),
      "Allow NFS from the scheduler task",
    );
    // Which interface carries the mount, the task's or the container host's, is a property of the
    // container agent rather than of this template, and the mount fails silently from the wrong one.
    // Both peers are groups this stack owns, and the file system policy below is what actually
    // limits access: only the scheduler task role, and only through its access point.
    fileSystemSecurityGroup.addIngressRule(
      this.hostSecurityGroup,
      ec2.Port.tcp(2049),
      "Allow NFS from the container host that mounts for the scheduler task",
    );

    const fileSystem = new efs.FileSystem(this.stack, "scheduler-pbs-file-system", {
      encrypted: true,
      securityGroup: fileSystemSecurityGroup,
      vpc: this.cluster.vpc,
      vpcSubnets: { subnets: this.cluster.privateSubnets },
    });
    fileSystem.applyRemovalPolicy(RemovalPolicy.RETAIN);
    const accessPoint = fileSystem.addAccessPoint("scheduler-pbs-access-point", {
      createAcl: {
        ownerGid: "0",
        ownerUid: "0",
        permissions: "0700",
      },
      path: "/pbs",
      posixUser: {
        gid: "0",
        uid: "0",
      },
    });
    accessPoint.applyRemovalPolicy(RemovalPolicy.RETAIN);

    const clientActions = [
      "elasticfilesystem:ClientMount",
      "elasticfilesystem:ClientWrite",
      "elasticfilesystem:ClientRootAccess",
    ];
    // The policy is a property of the file system and the access point refers to the file system,
    // so naming the access point here is a resource cycle. CloudFormation refuses the whole
    // template for it at change-set creation, and synthesis cannot see it. A mount can only
    // present an access point of the file system it is mounting, so requiring the shape of one
    // restricts exactly as naming this one did while this file system has the single access point
    // created below.
    // The policy is a property of the file system, so naming the file system in it resolves an
    // attribute of the resource the policy belongs to. CloudFormation counts that self reference
    // as a circular dependency and refuses the template. A file system policy applies only to the
    // file system carrying it, so the resource element does not have to name it.
    const OWN_FILE_SYSTEM = "*";
    const accessPointOfThisFileSystem =
      `arn:${Aws.PARTITION}:elasticfilesystem:${Aws.REGION}:${Aws.ACCOUNT_ID}:access-point/*`;
    fileSystem.addToResourcePolicy(
      new iam.PolicyStatement({
        actions: clientActions,
        conditions: {
          Bool: { "elasticfilesystem:AccessedViaMountTarget": "true" },
        },
        principals: [new iam.ArnPrincipal(this.taskRole("scheduler").roleArn)],
        resources: [OWN_FILE_SYSTEM],
      }),
    );
    fileSystem.addToResourcePolicy(
      new iam.PolicyStatement({
        actions: clientActions,
        conditions: {
          ArnNotEquals: { "aws:PrincipalArn": this.taskRole("scheduler").roleArn },
        },
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
          StringNotLike: {
            "elasticfilesystem:AccessPointArn": accessPointOfThisFileSystem,
          },
        },
        effect: iam.Effect.DENY,
        principals: [new iam.AnyPrincipal()],
        resources: [OWN_FILE_SYSTEM],
      }),
    );

    taskDefinition.addVolume({
      efsVolumeConfiguration: {
        authorizationConfig: {
          accessPointId: accessPoint.accessPointId,
          iam: "ENABLED",
        },
        fileSystemId: fileSystem.fileSystemId,
        rootDirectory: "/",
        transitEncryption: "ENABLED",
      },
      name: "scheduler-pbs",
    });
    container.addMountPoints({
      containerPath: SCHEDULER_PBS_HOME,
      readOnly: false,
      sourceVolume: "scheduler-pbs",
    });
  }

  /** Returns a digest-pinned image hosted in a private ECR repository. */
  private datadogImage(): string {
    const image = this.requiredString("ecs.datadog.image");
    const privateEcrDigest =
      /^[0-9]{12}\.dkr\.ecr(?:-fips)?\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?\/[^@]+@sha256:[0-9a-f]{64}$/;
    if (!privateEcrDigest.test(image)) {
      throw new Error("ecs.datadog.image must be a digest-pinned private ECR image");
    }
    return image;
  }

  /** Creates the optional host-network observability daemon. */
  private buildDatadogService(): void {
    if (!this.context.config.getBool("ecs.datadog.enabled", false)) return;

    const executionRole = this.buildExecutionRole("datadog-task-execution-role");
    // The agent needs no API calls of its own, so this role carries no policies. It exists because
    // a task definition without one gets a generated role whose trust has no account condition.
    const taskRole = new iam.Role(this.stack, "datadog-task-role", {
      assumedBy: this.ecsTasksPrincipal(),
      roleName: this.buildResourceName("datadog-task-role", true),
    });
    const taskDefinition = new ecs.Ec2TaskDefinition(this.stack, "datadog-task-definition", {
      executionRole,
      networkMode: ecs.NetworkMode.HOST,
      pidMode: ecs.PidMode.HOST,
      taskRole,
    });
    // Keeps the previous revision ACTIVE, so an agent image bump that fails has something to roll
    // back to.
    taskDefinition.applyRemovalPolicy(RemovalPolicy.RETAIN);
    const apiKey = secretsmanager.Secret.fromSecretCompleteArn(
      this.stack,
      "datadog-api-key-secret",
      this.requiredString("ecs.datadog.api_key_secret_arn"),
    );
    const datadogLogGroupName = `/${this.clusterName}/${this.context.config.moduleId("ecs")}/datadog`;
    const container = taskDefinition.addContainer("datadog-container", {
      environment: { DD_TAGS: `idea_cluster:${this.clusterName}` },
      image: ecs.ContainerImage.fromRegistry(this.datadogImage()),
      logging: this.adoptedLogDriver("datadog-logs", datadogLogGroupName, STREAM_PREFIX_DATADOG),
      memoryReservationMiB: 512,
      secrets: { DD_API_KEY: ecs.Secret.fromSecretsManager(apiKey) },
    });
    this.bindContainerToLogGroup(container, datadogLogGroupName);
    const mounts: Array<{ name: string; path: string; readOnly: boolean }> = [
      { name: "docker-socket", path: "/var/run/docker.sock", readOnly: false },
      { name: "proc", path: "/proc", readOnly: true },
      { name: "cgroup", path: "/sys/fs/cgroup", readOnly: true },
      { name: "datadog", path: "/var/run/datadog", readOnly: false },
    ];
    for (const mount of mounts) {
      taskDefinition.addVolume({ host: { sourcePath: mount.path }, name: mount.name });
      container.addMountPoints({
        containerPath: mount.path,
        readOnly: mount.readOnly,
        sourceVolume: mount.name,
      });
    }
    new ecs.Ec2Service(this.stack, "datadog-service", {
      capacityProviderStrategies: [{ capacityProvider: this.capacityProvider.capacityProviderName, weight: 1 }],
      cluster: this.ecsCluster,
      daemon: true,
      taskDefinition,
    });
  }

  /** Publishes the ECS service and target-group identities for the cutover stacks. */
  private buildClusterSettings(): void {
    const settings: Record<string, unknown> = {
      deployment_id: this.deploymentId,
      image: this.requiredString("ecs.image"),
      cluster_arn: this.ecsCluster.clusterArn,
      cluster_name: this.ecsCluster.clusterName,
      capacity_provider: this.capacityProvider.capacityProviderName,
      namespace_id: this.namespace.namespaceId,
      "dcv-gateway.certificate.certificate_secret_arn": this.gatewayCertificateSecretArn,
      "dcv-gateway.certificate.private_key_secret_arn": this.gatewayPrivateKeySecretArn,
    };
    for (const role of CONTAINER_ROLES) {
      const resources = this.roleResources[role];
      if (resources === undefined) throw new Error(`ECS service for ${role} was not built`);
      settings[`${role}.service_arn`] = resources.service.serviceArn;
      settings[`${role}.target_group_arns`] = resources.targetGroups.map((targetGroup) => targetGroup.targetGroupArn);
    }
    this.updateClusterSettings(settings);
  }
}

export function buildStack(props: StackBuildProps): void {
  new EcsStack(props);
}
