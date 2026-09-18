/**
 * Container control-plane stack.
 *
 * This stack supplies the shared ECS capacity: the cluster, the container hosts and their capacity
 * provider, the service-discovery namespace, the optional host observability daemon, and the
 * create-or-adopt provider for every agent-era log group.
 *
 * The application services are not here. Each one is created by the module stack that publishes
 * the settings its application reads, so a task starts only after its own module has written
 * `client_id` and the rest. That is the boot order the applications are written for, and it is the
 * only order a fresh install can satisfy: those rows do not exist until the module stack runs.
 */

import { Aws, CustomResource, RemovalPolicy } from "aws-cdk-lib";
import type { StackBuildProps } from "../app.ts";
import { IdeaBaseStack } from "../base-stack.ts";
import { CustomResourceProvider, LOG_RETENTION_DAYS } from "../constructs/common.ts";
import {
  DOGSTATSD_SOCKET,
  requireDigestPinnedImage,
  buildExecutionRole,
  grantInjectedSecret,
  ecsTasksPrincipal,
  storageMounts,
  type ContainerScope,
} from "../constructs/container.ts";
import { IdeaCodeAsset } from "../code-asset.ts";
import { ExistingSocaCluster } from "../constructs/existing-resources.ts";
import * as autoscaling from "aws-cdk-lib/aws-autoscaling";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";

/** Stream family for the optional host daemon. */
const STREAM_PREFIX_DATADOG = "datadog";
const NFS_MOUNT_OPTIONS = "nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport 0 0";
const LUSTRE_MOUNT_OPTIONS = "lustre defaults,noatime,flock,_netdev 0 0";
/** Agent-created groups use `cluster.cloudwatch_logs.retention_in_days`, which defaults to 90. */
const DEFAULT_AGENT_LOG_RETENTION_DAYS = 90;

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

  /** Resolved once from the configured host family: the host image and every task follow it. */
  private readonly hostArchitecture: ec2.InstanceArchitecture;
  private readonly ensuredLogGroups = new Map<string, CustomResource>();
  private logGroupEnsureProvider: CustomResourceProvider | undefined;

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

    this.ensureApplicationLogGroups();
    this.buildDatadogService();
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

  /** Returns a required string setting under the ECS module. */
  private requiredString(key: string): string {
    return this.context.config.getString(key, undefined, { required: true }) as string;
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
   *
   * The packaged policy template is scoped to the groups the cluster-manager stack ensures, so
   * this one carries its own statement over the cluster's own log-group tree.
   */
  private agentLogGroupProvider(): CustomResourceProvider {
    if (this.logGroupEnsureProvider !== undefined) return this.logGroupEnsureProvider;
    this.logGroupEnsureProvider = new CustomResourceProvider(this.context, "agent-log-group", this.stack, {
      ideaCodeAsset: new IdeaCodeAsset("idea_custom_resource_ensure_log_group"),
      resourceType: "EnsureLogGroup",
      policyStatements: [
        new iam.PolicyStatement({
          actions: ["logs:CreateLogGroup", "logs:PutRetentionPolicy"],
          resources: [
            `arn:${Aws.PARTITION}:logs:${Aws.REGION}:${Aws.ACCOUNT_ID}:log-group:/${this.clusterName}`,
            `arn:${Aws.PARTITION}:logs:${Aws.REGION}:${Aws.ACCOUNT_ID}:log-group:/${this.clusterName}/*`,
          ],
        }),
      ],
    });
    return this.logGroupEnsureProvider;
  }

  /** Ensures one agent log group exists with the cluster retention, then returns it. */
  private ensureAgentLogGroup(constructId: string, logGroupName: string): CustomResource {
    const existing = this.ensuredLogGroups.get(logGroupName);
    if (existing !== undefined) return existing;

    const properties: Record<string, string> = { LogGroupName: logGroupName };
    const retentionInDays = this.agentLogRetentionDays();
    // a string, not a number: the handler parses it the way the packaged contract states
    if (retentionInDays !== undefined) properties["RetentionInDays"] = String(retentionInDays);

    const resource = this.agentLogGroupProvider().invoke(constructId, properties);
    this.ensuredLogGroups.set(logGroupName, resource);
    return resource;
  }

  /**
   * Creates or adopts every group the five application tasks write to.
   *
   * The groups are agent-created on any cluster that has ever run hosts, and none of the module
   * stacks carries a create-or-adopt provider. This stack deploys before all of them and owns the
   * provider, so it ensures them by name and the module stacks write to them by name.
   */
  private ensureApplicationLogGroups(): void {
    const config = this.context.config;
    const clusterManagerId = config.moduleId("cluster-manager");
    const schedulerId = config.moduleId("scheduler");
    const vdcId = config.moduleId("virtual-desktop-controller");
    const groups: Array<[string, string]> = [
      ["cluster-manager-logs-ensure", `/${this.clusterName}/${clusterManagerId}`],
      ["scheduler-logs-ensure", `/${this.clusterName}/${schedulerId}`],
      ["scheduler-openpbs-logs-ensure", `/${this.clusterName}/${schedulerId}/openpbs`],
      ["vdc-controller-logs-ensure", `/${this.clusterName}/${vdcId}/controller`],
      ["dcv-broker-logs-ensure", `/${this.clusterName}/${vdcId}/dcv-broker`],
      ["dcv-gateway-logs-ensure", `/${this.clusterName}/${vdcId}/dcv-connection-gateway`],
    ];
    for (const [constructId, logGroupName] of groups) this.ensureAgentLogGroup(constructId, logGroupName);
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
    // Managed termination protection is what stops a scale-in killing a task mid-flight, and
    // enabling it requires scale-in protection on the group. Nothing removes that protection when
    // the group is meant to go away, so the group sits at desired zero with every instance still
    // in service and CloudFormation waits out its own timeout. The handler runs on Delete, the
    // only moment our code is in the loop during a rollback, and clears both the group default
    // and the instances that carry the flag already.
    const provider = new CustomResourceProvider(this.context, "host-scale-in-release", this.stack, {
      ideaCodeAsset: new IdeaCodeAsset("idea_custom_resource_release_scale_in_protection"),
      lambdaTimeoutSeconds: 300,
      policyStatements: [
        new iam.PolicyStatement({
          actions: ["autoscaling:DescribeAutoScalingGroups"],
          resources: ["*"],
        }),
        new iam.PolicyStatement({
          actions: ["autoscaling:SetInstanceProtection", "autoscaling:UpdateAutoScalingGroup"],
          resources: [
            `arn:${Aws.PARTITION}:autoscaling:${Aws.REGION}:${Aws.ACCOUNT_ID}:autoScalingGroup:*:autoScalingGroupName/${this.hostAutoScalingGroup.autoScalingGroupName}`,
          ],
        }),
      ],
      resourceType: "ReleaseScaleInProtection",
    });
    this.scaleInRelease = provider.invoke("host-scale-in-release", {
      AutoScalingGroupName: this.hostAutoScalingGroup.autoScalingGroupName,
    });
    this.scaleInRelease.node.addDependency(this.hostAutoScalingGroup);
  }

  /** Builds the ECS host group and its metadata-isolating launch template. */
  private buildHostAutoScalingGroup(): autoscaling.AutoScalingGroup {
    const userData = ec2.UserData.forLinux();
    userData.addCommands(
      // Shared storage first: the cluster name is written last, so a host whose mounts failed
      // never registers and never receives a task.
      ...this.hostStorageCommands(),
      "mkdir -p /etc/ecs",
      `echo ECS_CLUSTER=${this.ecsCluster.clusterName} >> /etc/ecs/ecs.config`,
      "echo ECS_AWSVPC_BLOCK_IMDS=true >> /etc/ecs/ecs.config",
      "install -d -o root -g root -m 0755 /var/run/datadog",
      // Tasks inherit the host's resolver file, and the container-optimised image's
      // systemd-resolved stub carries no search domain. The desktop agents advertise their hosts by
      // short name, which the gateway then cannot resolve, so the VPC's domain is made a search
      // domain here. (An IDEA host gets it from DHCP; this image does not apply it.)
      "mkdir -p /etc/systemd/resolved.conf.d",
      `printf '[Resolve]\\nDomains=%s\\n' "${Aws.REGION}.compute.internal" > /etc/systemd/resolved.conf.d/idea-search-domain.conf`,
      "systemctl restart systemd-resolved",
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
    // Host replacement needs scheduler draining and remote mount checks before retirement.
    // Keep termination protection; follow docs/ECS-HOST-REPLACEMENT.md for existing hosts.
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
   * What the host mounts for the tasks to bind in: ONTAP and OpenZFS over NFS, Lustre with its
   * client. Each entry mirrors the retired host bootstrap's fstab line, so `mount_options` keeps
   * its fstab shape ("<type> <options> 0 0") and the source carries the export path. A mount that
   * fails stops the host before it joins the cluster: a task placed on it would otherwise write
   * into an empty local directory that dies with the host.
   */
  private hostStorageCommands(): string[] {
    const entries: Array<{ directory: string; line: string }> = [];
    let lustre = false;
    for (const mount of storageMounts(this.context.config)) {
      if (mount.hostPath === undefined) continue;
      const storage = this.context.config.getConfig(`shared-storage.${mount.name}`, {});
      if (!isRecord(storage)) continue;
      const provider = storage["provider"];
      const configured = storage["mount_options"];
      const options = typeof configured === "string" && configured.trim() !== "" ? configured.trim() : undefined;
      let source: string | undefined;
      let fallback = NFS_MOUNT_OPTIONS;
      if (provider === "fsx_lustre") {
        const lustreConfig = storage["fsx_lustre"];
        if (isRecord(lustreConfig) && typeof lustreConfig["dns"] === "string" && typeof lustreConfig["mount_name"] === "string") {
          source = `${lustreConfig["dns"]}@tcp:/${lustreConfig["mount_name"]}`;
          fallback = LUSTRE_MOUNT_OPTIONS;
          lustre = true;
        }
      } else if (provider === "fsx_netapp_ontap") {
        const ontap = storage["fsx_netapp_ontap"];
        const svm = isRecord(ontap) ? ontap["svm"] : undefined;
        const volume = isRecord(ontap) ? ontap["volume"] : undefined;
        if (isRecord(svm) && typeof svm["nfs_dns"] === "string" && isRecord(volume) && typeof volume["volume_path"] === "string") {
          source = `${svm["nfs_dns"]}:${volume["volume_path"]}`;
        }
      } else if (provider === "fsx_openzfs") {
        const openzfs = storage["fsx_openzfs"];
        if (isRecord(openzfs) && typeof openzfs["dns"] === "string" && typeof openzfs["volume_path"] === "string") {
          source = `${openzfs["dns"]}:${openzfs["volume_path"]}`;
        }
      }
      if (source === undefined) {
        throw new Error(
          `shared-storage.${mount.name}: ${String(provider)} needs its endpoint and path before the container hosts can mount it`,
        );
      }
      entries.push({ directory: mount.hostPath, line: `${source} ${mount.hostPath}/ ${options ?? fallback}` });
    }
    if (entries.length === 0) return [];
    return [
      ...(lustre ? ["dnf install -y lustre-client"] : []),
      "dnf install -y nfs-utils",
      ...entries.flatMap((entry) => [
        `mkdir -p ${entry.directory}`,
        `grep -qF "${entry.line}" /etc/fstab || echo "${entry.line}" >> /etc/fstab`,
      ]),
      "mount -a",
      ...entries.map(
        (entry) =>
          `mountpoint -q ${entry.directory} || { echo "idea: ${entry.directory} is not mounted; this host does not join the cluster" >&2; exit 1; }`,
      ),
    ];
  }

  /** Returns the agent image, which must be digest-pinned: the daemon holds the host's Docker socket. */
  private datadogImage(): string {
    return requireDigestPinnedImage(this.requiredString("ecs.datadog.image"), "ecs.datadog.image");
  }

  /** Creates the optional host-network observability daemon. */
  private buildDatadogService(): void {
    if (!this.context.config.getBool("ecs.datadog.enabled", false)) return;

    const scope = this.containerScope;
    const executionRole = buildExecutionRole(scope, "datadog-task-execution-role");
    // The agent needs no API calls of its own, so this role carries no policies. It exists because
    // a task definition without one gets a generated role whose trust has no account condition.
    const taskRole = new iam.Role(this.stack, "datadog-task-role", {
      assumedBy: ecsTasksPrincipal(scope),
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
    grantInjectedSecret(scope, executionRole, apiKey.secretArn);
    // The module's own id, not the module-set row: an upgrade announces this module in the
    // module set only after every stack has deployed (see `heldModuleSetEntries`).
    const datadogLogGroupName = `/${this.clusterName}/${this.moduleId}/datadog`;
    const container = taskDefinition.addContainer("datadog-container", {
      // The agent listens on UDP by default; the tasks send to the socket they mount, so the
      // agent has to open it, and origin detection tags each point with its sending container.
      environment: {
        DD_DOGSTATSD_ORIGIN_DETECTION: "true",
        DD_DOGSTATSD_SOCKET: DOGSTATSD_SOCKET,
        DD_TAGS: `idea_cluster:${this.clusterName}`,
      },
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
    // A daemon runs on every host, so it names no capacity provider: ECS refuses a strategy on
    // the DAEMON scheduling strategy, and without one CDK sets the plain EC2 launch type.
    const service = new ecs.Ec2Service(this.stack, "datadog-service", {
      cluster: this.ecsCluster,
      daemon: true,
      taskDefinition,
    });
    // The daemon is the likeliest thing in this stack to fail, and the release has to already
    // exist when it does. Depending on the host group alone is not enough: the two are created in
    // parallel, and a failing service cancels the release before it finishes, so the rollback has
    // nothing to clear the protection with.
    service.node.addDependency(this.scaleInRelease);
  }

  /**
   * Publishes the shared capacity every module stack needs to run a task on it. Nothing here names
   * a service: the module stacks own those.
   */
  private buildClusterSettings(): void {
    this.updateClusterSettings({
      deployment_id: this.deploymentId,
      image: this.requiredString("ecs.image"),
      cluster_arn: this.ecsCluster.clusterArn,
      cluster_name: this.ecsCluster.clusterName,
      capacity_provider: this.capacityProvider.capacityProviderName,
      cpu_architecture: cpuArchitecture(this.hostArchitecture),
      host_security_group_id: this.hostSecurityGroup.securityGroupId,
      namespace_id: this.namespace.namespaceId,
      namespace_name: this.namespace.namespaceName,
    });
  }
}

export function buildStack(props: StackBuildProps): void {
  new EcsStack(props);
}
