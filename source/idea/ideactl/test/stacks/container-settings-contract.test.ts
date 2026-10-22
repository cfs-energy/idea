/**
 * Container module configuration and published settings contracts.
 */

import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test, { after } from "node:test";
import { byType, resourcesOf, synthBastion, synthEcs, cleanupWorkdirs } from "../support/ecs-harness.ts";
import { addLogTailContainer, applicationContainerSettings, applicationTargetGroup, buildEc2Service, buildExecutionRole, buildTaskDefinition, type ContainerScope } from "../../src/cdk/constructs/container.ts";

after(cleanupWorkdirs);
import { App, Aws, Fn, Stack } from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { ContainerImage } from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";
import { Template } from "aws-cdk-lib/assertions";
import { renderPolicy } from "../../src/cdk/policy.ts";
import * as yaml from "js-yaml";

import { ClusterConfig, MODULE_METADATA } from "../../src/config/cluster-config.ts";
import { jinjaEnv, renderTemplate } from "../../src/config/jinja.ts";
import { makeContext } from "../../src/cdk/constructs/base.ts";
import { EcsStack } from "../../src/cdk/stacks/ecs.ts";
import type { SynthReads } from "../../src/cdk/synth-reads.ts";
import { loadValuesFile } from "../../src/config/values.ts";
import { ideaVersion } from "../../src/version.ts";
import { ECS_HOST_SETTINGS, ECS_TASK_SETTINGS } from "../support/ecs-settings.ts";

type Json = Record<string, unknown>;

const ACCOUNT = "123456789012";
const CLUSTER = "idea-test1";
const REGION = "us-east-2";
const templateDirectory = fileURLToPath(
  new URL("../../resources/config/templates/", import.meta.url),
);

const CONFIGURATION_KEYS = [
  "ecs.tasks.bastion-host.cpu",
  "ecs.tasks.bastion-host.desired",
  "ecs.tasks.bastion-host.memory",
  "ecs.datadog.api_key_secret_arn",
  "ecs.datadog.enabled",
  "ecs.datadog.image",
  "ecs.enabled",
  "ecs.hosts.instance_type",
  "ecs.hosts.max",
  "ecs.hosts.min",
  "ecs.hosts.volume_size",
  "ecs.image",
  "ecs.image_repositories.aws",
  "ecs.image_repositories.aws-us-gov",
  "ecs.tasks.cluster-manager.cpu",
  "ecs.tasks.cluster-manager.desired",
  "ecs.tasks.cluster-manager.memory",
  "ecs.tasks.dcv-broker.cpu",
  "ecs.tasks.dcv-broker.desired",
  "ecs.tasks.dcv-broker.memory",
  // Sizing of the gateway task, read by the desktop stack that builds it rather than by this one.
  "ecs.tasks.dcv-gateway.cpu",
  "ecs.tasks.dcv-gateway.desired",
  "ecs.tasks.dcv-gateway.memory",
  "ecs.tasks.scheduler.cpu",
  "ecs.tasks.scheduler.desired",
  "ecs.tasks.scheduler.memory",
  "ecs.tasks.vdc.cpu",
  "ecs.tasks.vdc.desired",
  "ecs.tasks.vdc.memory",
].sort();

/**
 * The shared capacity, and nothing that names a service or a target group. Each module stack
 * creates its own service from these rows, so a row removed here breaks a module stack's synthesis
 * rather than the container stack's.
 */
const PUBLISHED_SETTING_KEYS = [
  "capacity_provider",
  "cluster_arn",
  "cluster_name",
  // The processor architecture this stack resolved from its host family. The module stacks build
  // their tasks from it rather than resolving the family again.
  "cpu_architecture",
  "deployment_id",
  // The scheduler file system admits NFS from the container host that carries the mount.
  "host_security_group_id",
  "image",
  // The desktop stack registers the broker in this namespace.
  "namespace_id",
  "namespace_name",
].sort();

const SYNTH_READS: SynthReads = {
  callerIdentity: async () => ({ account: ACCOUNT, arn: "synthetic-caller" }),
  describeDomain: async () => {
    throw new Error("synthetic ECS synthesis does not read domain settings");
  },
  describeListener: async () => ({}),
  describeUserPool: async () => ({}),
  listServiceLinkedRoles: async () => [],
};

function isRecord(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireRecord(value: unknown, description: string): Json {
  if (!isRecord(value)) throw new TypeError(description);
  return value;
}

function flattenKeys(prefix: string, value: unknown): string[] {
  if (!isRecord(value)) return [prefix];
  return Object.entries(value).flatMap(([key, child]) => flattenKeys(`${prefix}.${key}`, child));
}

function renderSettings(enableEcs: boolean): Json {
  const workdir = mkdtempSync(join(tmpdir(), "ideactl-ecs-config-values-"));
  const valuesFile = join(workdir, "values.yml");
  try {
    writeFileSync(valuesFile, `enable_ecs: ${enableEcs}\n`);
    const values = loadValuesFile(valuesFile);
    const value = values["enable_ecs"];
    if (typeof value !== "boolean") throw new TypeError("enable_ecs must parse as a Boolean");

    const rendered = renderTemplate(jinjaEnv(templateDirectory), "ecs/settings.yml", {
      enable_ecs: value,
    });
    return requireRecord(yaml.load(rendered), "ECS settings template must render a YAML mapping");
  } finally {
    rmSync(workdir, { force: true, recursive: true });
  }
}

function syntheticArn(service: string, resource: string): string {
  const region = service === "iam" ? "" : REGION;
  return Fn.join("", [
    "arn:",
    Aws.PARTITION,
    ":",
    service,
    ":",
    region,
    ":",
    Aws.ACCOUNT_ID,
    ":",
    resource,
  ]);
}

function ecsConfig(): ClusterConfig {
  const values: Record<string, unknown> = {
    "cluster.aws.account_id": ACCOUNT,
    "cluster.aws.dns_suffix": "amazonaws.com",
    "cluster.aws.partition": "aws",
    "cluster.aws.region": REGION,
    "cluster.cluster_name": CLUSTER,
    "cluster.cluster_s3_bucket": "sample-cluster-bucket",
    "cluster.cluster_settings_lambda_arn": syntheticArn("lambda", "function:settings"),
    "cluster.iam.policies.amazon_ssm_managed_instance_core_arn": syntheticArn("iam", "policy/ssm"),
    "cluster.iam.policies.cloud_watch_agent_server_arn": syntheticArn("iam", "policy/cloudwatch"),
    "cluster.iam.roles": {},
    "cluster.load_balancers.internal_alb.load_balancer_dns_name": "internal.example.invalid",
    "cluster.network.private_subnets": ["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"],
    "cluster.network.security_groups.external-load-balancer": "sg-0123456789abcdef0",
    "cluster.network.security_groups.internal-load-balancer": "sg-0123456789abcdef1",
    "cluster.network.vpc_id": "vpc-0123456789abcdef0",
    "cluster.route53.private_hosted_zone_id": "ZTEST",
    "cluster-manager.iam_role_arn": syntheticArn("iam", "role/cluster-manager"),
    "cluster-manager.security_group_id": "sg-0123456789abcdef2",
    "ecs.datadog.api_key_secret_arn": syntheticArn("secretsmanager", "secret:metrics"),
    "ecs.datadog.enabled": false,
    "ecs.datadog.image": "registry.example.invalid/agent:1",
    ...ECS_HOST_SETTINGS,
    "ecs.image": "registry.example.invalid/control-plane@sha256:abcdef",
    ...ECS_TASK_SETTINGS,
    "global-settings.custom_tags": [],
    "global-settings.module_sets.default.cluster.module_id": "cluster",
    "global-settings.module_sets.default.cluster-manager.module_id": "cluster-manager",
    "global-settings.module_sets.default.directoryservice.module_id": "directoryservice",
    "global-settings.module_sets.default.ecs.module_id": "ecs",
    "global-settings.module_sets.default.identity-provider.module_id": "identity-provider",
    "global-settings.module_sets.default.scheduler.module_id": "scheduler",
    "global-settings.module_sets.default.shared-storage.module_id": "shared-storage",
    "global-settings.module_sets.default.virtual-desktop-controller.module_id": "vdc",
    "identity-provider.cognito.provider_url": "https://identity.example.invalid",
    "metrics.provider": "cloudwatch",
    "scheduler.iam_role_arn": syntheticArn("iam", "role/scheduler"),
    "scheduler.security_group_id": "sg-0123456789abcdef3",
    "shared-storage.apps.efs.file_system_id": "fs-apps",
    "shared-storage.apps.mount_dir": "/apps",
    "shared-storage.apps.provider": "efs",
    "shared-storage.data.efs.file_system_id": "fs-data",
    "shared-storage.data.mount_dir": "/data",
    "shared-storage.data.provider": "efs",
    "vdc.controller.security_group_id": "sg-0123456789abcdef4",
    "vdc.controller_iam_role_arn": syntheticArn("iam", "role/controller"),
    "vdc.dcv_broker.security_group_id": "sg-0123456789abcdef5",
    "vdc.dcv_broker_role_arn": syntheticArn("iam", "role/broker"),
    "vdc.dcv_connection_gateway.certificate.certificate_secret_arn":
      syntheticArn("secretsmanager", "secret:certificate"),
    "vdc.dcv_connection_gateway.certificate.private_key_secret_arn":
      syntheticArn("secretsmanager", "secret:key"),
    "vdc.dcv_connection_gateway.security_group_id": "sg-0123456789abcdef6",
    "vdc.dcv_connection_gateway_iam_role_arn":
      syntheticArn("iam", "role/gateway"),
  };
  return new ClusterConfig(Object.entries(values).map(([key, value]) => ({ key, value })));
}

function vpcContext(): Json {
  return {
    [`vpc-provider:account=${ACCOUNT}:filter.vpc-id=vpc-0123456789abcdef0:region=${REGION}:returnAsymmetricSubnets=true`]: {
      availabilityZones: [],
      ownerAccountId: ACCOUNT,
      subnetGroups: [
        {
          name: "private",
          subnets: [
            {
              availabilityZone: "us-east-2a",
              cidr: "192.0.2.0/25",
              routeTableId: "rtb-0123456789abcdef0",
              subnetId: "subnet-0123456789abcdef0",
            },
            {
              availabilityZone: "us-east-2b",
              cidr: "192.0.2.128/25",
              routeTableId: "rtb-0123456789abcdef1",
              subnetId: "subnet-0123456789abcdef1",
            },
          ],
          type: "Private",
        },
      ],
      vpcCidrBlock: "192.0.2.0/24",
      vpcId: "vpc-0123456789abcdef0",
    },
  };
}

function publishedSettings(): Json {
  const outdir = mkdtempSync(join(tmpdir(), "ideactl-ecs-config-synth-"));
  try {
    const app = new App({
      context: { "aws:cdk:enable-path-metadata": true, ...vpcContext() },
      outdir,
    });
    new EcsStack({
      app,
      ctx: makeContext({
        awsRegion: REGION,
        config: ecsConfig(),
        moduleId: "ecs",
        releaseVersion: ideaVersion(),
        synthReads: SYNTH_READS,
      }),
      deploymentId: "synthetic-deployment",
      env: { account: ACCOUNT, region: REGION },
      moduleName: "ecs",
      terminationProtection: true,
    });
    const template = requireRecord(
      app.synth().getStackByName(`${CLUSTER}-ecs`).template,
      "ECS stack must synthesize a template",
    );
    const resources = requireRecord(template["Resources"], "ECS template must contain resources");
    const resource = Object.values(resources).find((candidate) => {
      const record = requireRecord(candidate, "ECS resource must be a mapping");
      return record["Type"] === "Custom::ClusterSettings";
    });
    const settingsResource = requireRecord(resource, "ECS template must publish cluster settings");
    const properties = requireRecord(
      settingsResource["Properties"],
      "ECS settings resource must contain properties",
    );
    return requireRecord(properties["settings"], "ECS settings resource must contain settings");
  } finally {
    rmSync(outdir, { force: true, recursive: true });
  }
}

test("the ECS module resolves by name and id at priority 4.5", () => {
  const config = new ClusterConfig(
    [{ key: "global-settings.module_sets.default.ecs.module_id", value: "ecs-plane" }],
    [{ module_id: "ecs-plane", name: "ecs", type: "stack" }],
  );

  assert.equal(config.moduleId("ecs"), "ecs-plane");
  assert.equal(config.moduleInfoById("ecs-plane")?.name, "ecs");
  assert.equal(config.moduleInfoById("ecs"), undefined);
  assert.deepEqual(MODULE_METADATA.find((entry) => entry.name === "ecs"), {
    name: "ecs",
    title: "ECS",
    type: "stack",
    deployment_priority: 4.5,
  });
  assert.equal(config.modules()[0]?.deployment_priority, 4.5);
});

test("enable_ecs switches the rendered ECS setting on and off", () => {
  assert.equal(renderSettings(false)["enabled"], false);
  assert.equal(renderSettings(true)["enabled"], true);
});

test("the ECS template declares every configuration key", () => {
  const settings = renderSettings(true);
  const keys = flattenKeys("ecs", settings).sort();
  assert.deepEqual(keys, CONFIGURATION_KEYS);
  assert.equal(keys.includes("ecs.awsvpc_trunking"), false);
});

test("the ECS stack publishes every cutover setting by name", () => {
  assert.deepEqual(Object.keys(publishedSettings()).sort(), PUBLISHED_SETTING_KEYS);
});

test("the host pool takes its bounds from the rows and drains managed instances", () => {
  // Four hosts at rest, five at most: a surge that does not fit borrows the fifth for the length
  // of the deployment and managed scaling gives it back.
  const resources = resourcesOf(synthEcs(true));
  const group = byType(resources, "AWS::AutoScaling::AutoScalingGroup")[0]![1];
  assert.equal(group.Properties.MinSize, String(ECS_HOST_SETTINGS["ecs.hosts.min"]));
  assert.equal(group.Properties.MaxSize, String(ECS_HOST_SETTINGS["ecs.hosts.max"]));
  assert.equal(group.UpdatePolicy?.AutoScalingRollingUpdate, undefined);
  assert.equal(group.UpdatePolicy?.AutoScalingReplacingUpdate, undefined);
  assert.equal(group.Properties.NewInstancesProtectedFromScaleIn, true);
  const provider = byType(resources, "AWS::ECS::CapacityProvider")[0]![1].Properties.AutoScalingGroupProvider;
  assert.equal(provider.ManagedScaling.TargetCapacity, 100);
  assert.equal(provider.ManagedDraining, "ENABLED");
  assert.equal(provider.ManagedTerminationProtection, "ENABLED");
  const service = byType(resources, "AWS::ECS::Service")[0]![1].Properties;
  assert.deepEqual({ ...service.DeploymentConfiguration, Alarms: undefined }, {
    Alarms: undefined,
    MinimumHealthyPercent: 50, MaximumPercent: 100,
    DeploymentCircuitBreaker: { Enable: true, Rollback: true },
  });
  assert.equal(service.HealthCheckGracePeriodSeconds, undefined);
  const container = byType(resources, "AWS::ECS::TaskDefinition")[0]![1].Properties.ContainerDefinitions[0];
  assert.deepEqual(container.HealthCheck.Command, ["CMD", "agent", "health"]);
  assert.equal(container.StopTimeout, 30);
});

test("bastion keeps both healthy tasks while both SSH replacements start", () => {
  const resources = resourcesOf(synthBastion());
  const service = byType(resources, "AWS::ECS::Service")[0]![1].Properties;
  assert.equal(service.DesiredCount, 2);
  assert.deepEqual({ ...service.DeploymentConfiguration, Alarms: undefined }, {
    Alarms: undefined,
    // Both replacements start at once and neither old task stops until a new one answers
    // ssh-keyscan, so the login node never drops below two.
    MinimumHealthyPercent: 100, MaximumPercent: 200,
    DeploymentCircuitBreaker: { Enable: true, Rollback: true },
  });
  assert.equal(service.HealthCheckGracePeriodSeconds, 270);
  // One per host, on a directory-joined host.
  assert.deepEqual(service.PlacementConstraints, [{ Type: "distinctInstance" }, { Type: "memberOf", Expression: "attribute:idea.directory == joined" }]);
  // Zone spread first, then pack by memory, so the borrowed host can empty and be returned.
  assert.deepEqual(service.PlacementStrategies, [
    { Type: "spread", Field: "attribute:ecs.availability-zone" },
    { Type: "binpack", Field: "MEMORY" },
  ]);
  const container = byType(resources, "AWS::ECS::TaskDefinition")[0]![1].Properties.ContainerDefinitions[0];
  assert.match(container.HealthCheck.Command.join(" "), /ssh-keyscan/);
  assert.equal(container.StopTimeout, 30);
  const group = byType(resources, "AWS::ElasticLoadBalancingV2::TargetGroup")[0]![1].Properties;
  assert.equal(group.HealthCheckProtocol, "TCP");
  assert.equal(group.HealthCheckPort, "22");
  // 30s keeps sshd logs quiet; a new target turns healthy after ~60s, inside the 270s grace.
  assert.equal(group.HealthCheckIntervalSeconds, 30);
  assert.equal(group.HealthyThresholdCount, 2);
  assert.equal(group.TargetGroupAttributes.find((entry: { Key: string }) => entry.Key === "deregistration_delay.timeout_seconds").Value, "30");
});

test("readiness checks use the application endpoints on every role", () => {
  for (const role of ["cluster-manager", "vdc", "dcv-broker", "dcv-gateway", "bastion-host"] as const) {
    const settings = applicationContainerSettings(role);
    assert.equal(settings.healthCheck?.interval?.toSeconds(), 5);
    assert.equal(settings.stopTimeout?.toSeconds(), 30);
    const command = settings.healthCheck!.command.join(" ");
    assert.match(command, role === "dcv-broker" ? /8444\/health.*8445\/health.*8446\/health/ : role === "dcv-gateway" ? /8989/ : role === "bastion-host" ? /ssh-keyscan/ : /https:\/\/localhost:8443\/healthcheck/);
  }
});

test("replicated services surge in full, the scheduler stops first, and targets drain for the stop allowance", () => {
  const config = ecsConfig();
  config.setEntry("ecs.cluster_name", `${CLUSTER}-ecs`);
  config.setEntry("ecs.capacity_provider", `${CLUSTER}-ecs-capacity-provider`);
  config.setEntry("ecs.cpu_architecture", "ARM64");
  const app = new App();
  const stack = new Stack(app, "services", { env: { account: ACCOUNT, region: REGION } });
  const vpc = new ec2.Vpc(stack, "vpc", { maxAzs: 2, natGateways: 0 });
  const scope: ContainerScope = {
    ctx: makeContext({ awsRegion: REGION, config, moduleId: "cluster-manager", releaseVersion: ideaVersion(), synthReads: SYNTH_READS }),
    stack, vpc, privateSubnets: vpc.isolatedSubnets.length > 0 ? vpc.isolatedSubnets : vpc.publicSubnets,
  };
  const service = (id: string, desiredCount: number, singleWriter?: boolean) => {
    const role = new iam.Role(stack, `${id}-role`, { assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com") });
    const taskDefinition = buildTaskDefinition(scope, `${id}-task`, { executionRole: buildExecutionRole(scope, `${id}-execution`, `${id}-execution`), taskRole: role });
    taskDefinition.addContainer(`${id}-container`, { image: ContainerImage.fromRegistry("registry.example.invalid/image:1"), memoryLimitMiB: 512, ...applicationContainerSettings("cluster-manager") });
    buildEc2Service(scope, { constructId: id, serviceName: id, taskDefinition, desiredCount, securityGroups: [], singleWriter, distinctInstances: singleWriter !== true, dependencies: [] });
    return taskDefinition;
  };
  const pair = service("pair", 2);
  pair.addVolume({ name: "logs" });
  addLogTailContainer(scope, pair, {
    containerId: "pair-logs", directories: ["/logs"], logGroupName: "/pair", logGroupConstructId: "pair-log-group",
    streamPrefix: "application", sourceVolume: "logs", containerPath: "/logs", readOnly: true,
  });
  service("trio", 3);
  service("writer", 1, true);
  applicationTargetGroup(scope, { constructId: "default-drain", targetGroupName: "default-drain", port: 8443, healthCheckPath: "/healthcheck" });

  const resources = Template.fromStack(stack).toJSON().Resources as Record<string, Json>;
  const services = Object.fromEntries(Object.values(resources)
    .filter((resource) => resource["Type"] === "AWS::ECS::Service")
    .map((resource) => {
      const properties = resource["Properties"] as Record<string, any>;
      return [properties["ServiceName"], properties];
    }));
  for (const name of ["pair", "trio"]) {
    // A disruption budget of zero with a full surge: every replacement starts at once and no old
    // task stops before a new one is healthy.
    assert.equal(services[name].DeploymentConfiguration.MinimumHealthyPercent, 100, name);
    assert.equal(services[name].DeploymentConfiguration.MaximumPercent, 200, name);
    assert.deepEqual(services[name].DeploymentConfiguration.DeploymentCircuitBreaker, { Enable: true, Rollback: true }, name);
    // One replica per host, mid-rollout too.
    assert.deepEqual(services[name].PlacementConstraints, [{ Type: "distinctInstance" }], name);
    assert.deepEqual(services[name].PlacementStrategies, [
      { Type: "spread", Field: "attribute:ecs.availability-zone" },
      { Type: "binpack", Field: "MEMORY" },
    ], name);
  }
  // Two batch servers cannot hold the state directory at once: the old one stops first.
  assert.equal(services["writer"].DeploymentConfiguration.MinimumHealthyPercent, 0);
  assert.equal(services["writer"].DeploymentConfiguration.MaximumPercent, 100);
  assert.deepEqual(services["writer"].PlacementConstraints ?? [], [], "the one batch server is not spread");
  const group = Object.values(resources).find((resource) => resource["Type"] === "AWS::ElasticLoadBalancingV2::TargetGroup")!;
  const drain = ((group["Properties"] as Json)["TargetGroupAttributes"] as Array<{Key: string, Value: string}>)
    .find((entry) => entry.Key === "deregistration_delay.timeout_seconds")?.Value;
  // The log follower is a bash loop: as PID 1 it ignored SIGTERM and every stop ended in SIGKILL.
  // Docker's init takes PID 1 and passes the signal to the loop and its tails.
  const follower = (Object.values(resources).find((resource) => resource["Type"] === "AWS::ECS::TaskDefinition"
    && JSON.stringify(resource).includes("pair-logs"))!["Properties"] as Json)["ContainerDefinitions"] as Json[];
  const logs = follower.find((container) => container["Name"] === "pair-logs")!;
  assert.equal((logs["LinuxParameters"] as Json)["InitProcessEnabled"], true);
  assert.ok((logs["Environment"] as Json[]).some((entry) => entry["Name"] === "TINI_KILL_PROCESS_GROUP" && entry["Value"] === "1"));
  // A stopping task drains for as long as its container then gets to stop, not five minutes.
  assert.equal(drain, "30");
  assert.equal(applicationContainerSettings("cluster-manager").stopTimeout?.toSeconds(), 30);
});

test("four resting hosts hold every service's full surge at once, one replica per host", () => {
  const settings = ECS_TASK_SETTINGS as Record<string, number>;
  const roles = ["bastion-host", "cluster-manager", "vdc", "scheduler", "dcv-broker", "dcv-gateway"];
  const tasks = roles.map(role => ({
    role, cpu: settings[`ecs.tasks.${role}.cpu`]!,
    memory: settings[`ecs.tasks.${role}.memory`]! + (role === "bastion-host" ? 0 : role === "scheduler" ? 64 : 32),
    desired: settings[`ecs.tasks.${role}.desired`]!,
  }));
  const minimum = ECS_HOST_SETTINGS["ecs.hosts.min"];
  assert.equal(minimum, 4);
  assert.equal(ECS_HOST_SETTINGS["ecs.hosts.max"], 5);
  // the configured host type's capacity, less the agent and OS reserve the old fixed numbers kept
  const capacity: Record<string, {memory: number, cpu: number}> = {
    "m7g.large": {memory: 8192, cpu: 2048},
    "m7g.xlarge": {memory: 16384, cpu: 4096},
    "m7g.2xlarge": {memory: 32768, cpu: 8192},
  };
  const host = capacity[ECS_HOST_SETTINGS["ecs.hosts.instance_type"] as string];
  assert.ok(host, "the default host type has a known capacity");
  const usableMemory = host.memory - 512 - 512;
  const usableCpu = host.cpu - 2;
  type Slot = {role: string, cpu: number, memory: number};
  // The distinct-instance constraint counts old and new tasks alike: no host may hold two tasks
  // of one role, resting or mid-rollout.
  function fits(slots: Slot[], hostCount: number): boolean {
    const hosts = Array.from({length: hostCount}, () => ({memory: 0, cpu: 0, roles: new Set<string>()}));
    const ordered = [...slots].sort((a, b) => b.memory - a.memory);
    function place(index: number): boolean {
      const task = ordered[index];
      if (task === undefined) return true;
      for (const host of hosts) {
        if (host.roles.has(task.role) || host.memory + task.memory > usableMemory || host.cpu + task.cpu > usableCpu) continue;
        host.roles.add(task.role); host.memory += task.memory; host.cpu += task.cpu;
        if (place(index + 1)) return true;
        host.roles.delete(task.role); host.memory -= task.memory; host.cpu -= task.cpu;
      }
      return false;
    }
    return place(0);
  }
  const resting: Slot[] = tasks.flatMap(task => Array.from({length: task.desired}, () => task));
  assert.ok(fits(resting, minimum), "the resting tasks fit the configured minimum");
  // An upgrade deploys the module stacks of one priority together, so every replicated service
  // surges at once: each starts a full set of replacements beside its old tasks, all on distinct
  // hosts. The scheduler stops before its replacement starts, so its one task is counted once.
  // None of this may wait for the spare host: one that launches and joins takes minutes.
  const surge: Slot[] = tasks.filter(task => task.role !== "scheduler")
    .flatMap(task => Array.from({length: task.desired}, () => task));
  assert.ok(fits([...resting, ...surge], minimum), "every surge fits the resting hosts at once");
  // Three hosts cannot: a two-task service's surge needs four distinct hosts.
  assert.equal(fits([...resting, ...surge], 3), false, "three hosts hold no full surge");
});


test("synthesized cluster-manager policy permits retirement only on the outbox prefix", () => {
  const stack = new Stack();
  new iam.ManagedPolicy(stack, "collector-policy", {
    document: iam.PolicyDocument.fromJson(renderPolicy("cluster-manager.yml", {config: ecsConfig(), moduleId: "cluster-manager"})),
  });
  const policies = Template.fromStack(stack).findResources("AWS::IAM::ManagedPolicy");
  const policy = Object.values(policies)[0]!;
  const statements = policy.Properties.PolicyDocument.Statement as Array<{Action: string | string[]; Resource: string | string[]; Effect: string}>;
  const deletes = statements.filter(statement => [statement.Action].flat().includes("s3:DeleteObject"));
  assert.equal(deletes.length, 1);
  assert.equal(deletes[0]!.Effect, "Allow");
  assert.deepEqual([deletes[0]!.Resource].flat(), ["arn:aws:s3:::sample-cluster-bucket/metrics/outbox/*"]);
});
