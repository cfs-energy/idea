import { Stack, type StackProps, RemovalPolicy } from "aws-cdk-lib";
import type { Construct } from "constructs";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { DOGSTATSD_SOCKET, requireDigestPinnedImage } from "../constructs/container.ts";

export interface CostCollectorSettings {
  clusterName: string;
  controlPlaneImage: string;
  agentImage: string;
  datadogApiKeySecretArn: string;
  intervalHours?: number;
  lookbackDays?: number;
  moduleTag?: string;
  projectTag?: string;
  ownerTag?: string;
  byAccount?: boolean;
}

export interface CostCollectorNetwork {
  vpcId: string;
  subnetIds: string[];
  publicSubnets: boolean;
}

export class CostCollectorStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps & CostCollectorSettings & CostCollectorNetwork) {
    super(scope, id, props);
    const agentImage = requireDigestPinnedImage(props.agentImage, "agent image");
    for (const value of [props.intervalHours ?? 6, props.lookbackDays ?? 3]) {
      if (!Number.isInteger(value) || value < 1) throw new Error("interval and lookback must be positive integers");
    }
    if (props.subnetIds.length === 0) throw new Error("at least one subnet is required");
    const logGroup = new logs.LogGroup(this, "logs", {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const cluster = new ecs.CfnCluster(this, "cluster");
    const principal = () => new iam.ServicePrincipal("ecs-tasks.amazonaws.com", {
      conditions: { StringEquals: { "aws:SourceAccount": this.account } },
    });
    const taskRole = new iam.Role(this, "task-role", { assumedBy: principal() });
    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: ["ce:GetCostAndUsage", "ce:GetTags", "ce:GetDimensionValues"],
      resources: ["*"],
    }));
    const executionRole = new iam.Role(this, "execution-role", {
      assumedBy: principal(),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AmazonECSTaskExecutionRolePolicy")],
    });
    // The ARN does not reveal the encryption key, and importing the secret cannot grant it.
    // Restrict decryption to this secret through Secrets Manager so custom keys also work.
    executionRole.addToPolicy(new iam.PolicyStatement({
      actions: ["kms:Decrypt"],
      resources: ["*"],
      conditions: { StringEquals: {
        "kms:ViaService": `secretsmanager.${this.region}.${this.urlSuffix}`,
        "kms:EncryptionContext:SecretARN": props.datadogApiKeySecretArn,
      } },
    }));
    const task = new ecs.FargateTaskDefinition(this, "task", {
      cpu: 256,
      memoryLimitMiB: 1024,
      executionRole,
      taskRole,
    });
    task.addVolume({ name: "datadog" });
    const secret = secretsmanager.Secret.fromSecretCompleteArn(this, "api-key", props.datadogApiKeySecretArn);
    const agent = task.addContainer("datadog", {
      image: ecs.ContainerImage.fromRegistry(agentImage),
      environment: {
        ECS_FARGATE: "true",
        DD_DOGSTATSD_ORIGIN_DETECTION: "true",
        DD_DOGSTATSD_SOCKET: DOGSTATSD_SOCKET,
        DD_TAGS: `idea_cluster:${props.clusterName}`,
      },
      secrets: { DD_API_KEY: ecs.Secret.fromSecretsManager(secret) },
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: "datadog" }),
      healthCheck: { command: ["CMD-SHELL", "agent health"] },
    });
    const collector = task.addContainer("cost-metrics", {
      image: ecs.ContainerImage.fromRegistry(props.controlPlaneImage),
      environment: {
        IDEA_CONTAINER_ROLE: "cost-metrics",
        IDEA_CLUSTER_NAME: props.clusterName,
        AWS_DEFAULT_REGION: this.region,
        DD_DOGSTATSD_URL: `unix://${DOGSTATSD_SOCKET}`,
        IDEA_COST_METRICS_ENABLED: "true",
        IDEA_COST_METRICS_INTERVAL_HOURS: String(props.intervalHours ?? 6),
        IDEA_COST_METRICS_LOOKBACK_DAYS: String(props.lookbackDays ?? 3),
        IDEA_COST_METRICS_MODULE_TAG: props.moduleTag ?? "idea:ModuleId",
        IDEA_COST_METRICS_PROJECT_TAG: props.projectTag ?? "idea:Project",
        IDEA_COST_METRICS_OWNER_TAG: props.ownerTag ?? "idea:JobOwner",
        IDEA_COST_METRICS_BY_ACCOUNT: String(props.byAccount ?? false),
      },
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: "cost-metrics" }),
    });
    collector.addContainerDependencies({ container: agent, condition: ecs.ContainerDependencyCondition.HEALTHY });
    for (const container of [agent, collector]) {
      container.addMountPoints({ containerPath: "/var/run/datadog", sourceVolume: "datadog", readOnly: container === collector });
    }
    const securityGroup = new ec2.CfnSecurityGroup(this, "security-group", {
      groupDescription: "Cost Explorer and metrics outbound access",
      vpcId: props.vpcId,
      securityGroupEgress: [{ ipProtocol: "-1", cidrIp: "0.0.0.0/0" }],
    });
    const service = new ecs.CfnService(this, "service", {
      cluster: cluster.ref,
      taskDefinition: task.taskDefinitionArn,
      desiredCount: 1,
      launchType: "FARGATE",
      // Two collectors would publish the same daily counts during a rolling update.
      // Stop the old task first so the no-op lock remains safe across revisions.
      deploymentConfiguration: { minimumHealthyPercent: 0, maximumPercent: 100 },
      networkConfiguration: {
        awsvpcConfiguration: {
          subnets: props.subnetIds,
          securityGroups: [securityGroup.attrGroupId],
          assignPublicIp: props.publicSubnets ? "ENABLED" : "DISABLED",
        },
      },
    });
    service.node.addDependency(taskRole, executionRole);
  }
}
