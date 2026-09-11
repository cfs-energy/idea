/**
 * ECS stack synthesis checks.
 *
 * The test uses only synthetic settings and a replay-free VPC context. It
 * asserts the deployment controls whose removal would make a container
 * cutover unsafe.
 */

import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import { App, Aws, Fn } from "aws-cdk-lib";

import { ClusterConfig } from "../../src/config/cluster-config.ts";
import { checkAwsvpcTrunking } from "../../src/cli/commands/upgrade.ts";
import { makeContext } from "../../src/cdk/constructs/base.ts";
import { EcsStack } from "../../src/cdk/stacks/ecs.ts";
import type { SynthReads } from "../../src/cdk/synth-reads.ts";
import { ideaVersion } from "../../src/version.ts";

type Json = Record<string, unknown>;

const ACCOUNT = "123456789012";
const CLUSTER = "idea-test1";
const REGION = "us-east-2";
const workdirs: string[] = [];

function syntheticArn(service: string, resource: string): string {
  const region = service === "iam" ? "" : REGION;
  return Fn.join("", ["arn:", Aws.PARTITION, ":", service, ":", region, ":", Aws.ACCOUNT_ID, ":", resource]);
}

const SYNTH_READS: SynthReads = {
  callerIdentity: async () => ({ account: ACCOUNT, arn: "synthetic-caller" }),
  describeDomain: async () => {
    throw new Error("synthetic ECS synthesis does not read domain settings");
  },
  describeListener: async () => ({}),
  describeUserPool: async () => ({}),
  listServiceLinkedRoles: async () => [],
};

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

function settings(datadogEnabled = false): ClusterConfig {
  const values: Record<string, unknown> = {
    "cluster.aws.account_id": ACCOUNT,
    "cluster.aws.dns_suffix": "amazonaws.com",
    "cluster.aws.partition": "aws",
    "cluster.aws.region": REGION,
    "cluster.cluster_name": CLUSTER,
    "cluster.cluster_s3_bucket": "sample-cluster-bucket",
    "cluster.cluster_settings_lambda_arn": syntheticArn("lambda", "function:synthetic-settings-handler"),
    // The container stack attaches its own target groups to the listeners that serve them, so it
    // reads the endpoint handler and the four listeners. All are cluster-stack outputs, published
    // at priority two and consumed here at four and a half.
    "cluster.cluster_endpoints_lambda_arn": syntheticArn("lambda", "function:synthetic-endpoints-handler"),
    "cluster.load_balancers.external_alb.https_listener_arn": syntheticArn("elasticloadbalancing", "listener/app/external/0123456789abcdef/0123456789abcdef"),
    "cluster.load_balancers.internal_alb.https_listener_arn": syntheticArn("elasticloadbalancing", "listener/app/internal/0123456789abcdef/0123456789abcdef"),
    "cluster.load_balancers.internal_alb.dcv_broker_client_listener_arn": syntheticArn("elasticloadbalancing", "listener/app/internal/0123456789abcdef/1123456789abcdef"),
    "cluster.load_balancers.internal_alb.dcv_broker_agent_listener_arn": syntheticArn("elasticloadbalancing", "listener/app/internal/0123456789abcdef/2123456789abcdef"),
    "cluster.load_balancers.internal_alb.dcv_broker_gateway_listener_arn": syntheticArn("elasticloadbalancing", "listener/app/internal/0123456789abcdef/3123456789abcdef"),
    "cm.endpoints.external.priority": 12,
    "cm.endpoints.external.path_patterns": ["/cluster-manager/*"],
    "cm.endpoints.internal.priority": 12,
    "cm.endpoints.internal.path_patterns": ["/cluster-manager/*"],
    "scheduler.endpoints.external.priority": 15,
    "scheduler.endpoints.external.path_patterns": ["/scheduler/*"],
    "scheduler.endpoints.internal.priority": 15,
    "scheduler.endpoints.internal.path_patterns": ["/scheduler/*"],
    "vdc.controller.endpoints.external.priority": 13,
    "vdc.controller.endpoints.external.path_patterns": ["/vdc/*"],
    "vdc.controller.endpoints.internal.priority": 13,
    "vdc.controller.endpoints.internal.path_patterns": ["/vdc/*"],
    "cluster.iam.policies.amazon_ssm_managed_instance_core_arn": syntheticArn("iam", "policy/synthetic-ssm-policy"),
    "cluster.iam.policies.cloud_watch_agent_server_arn": syntheticArn("iam", "policy/synthetic-cloudwatch-policy"),
    "cluster.iam.roles": {},
    "cluster.load_balancers.internal_alb.load_balancer_dns_name": "internal.example.invalid",
    "cluster.network.private_subnets": ["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"],
    "cluster.network.security_groups.external-load-balancer": "sg-0123456789abcdef0",
    "cluster.network.security_groups.internal-load-balancer": "sg-0123456789abcdef1",
    "cluster.network.vpc_id": "vpc-0123456789abcdef0",
    "cluster.route53.private_hosted_zone_id": "Z0123456789ABCDEF",
    "cm.iam_role_arn": syntheticArn("iam", "role/synthetic-cluster-manager-role"),
    "cm.security_group_id": "sg-0123456789abcdef2",
    "ecs.datadog.api_key_secret_arn": syntheticArn("secretsmanager", "secret:synthetic-datadog-secret"),
    "ecs.datadog.enabled": datadogEnabled,
    "ecs.datadog.image": `${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/observability-agent@sha256:${"a".repeat(64)}`,
    "ecs.hosts.instance_type": "m7g.large",
    "ecs.hosts.max": 4,
    "ecs.hosts.min": 3,
    "ecs.hosts.volume_size": 60,
    "ecs.image": "registry.example.invalid/control-plane@sha256:abcdef",
    "ecs.tasks.cluster-manager.cpu": 256,
    "ecs.tasks.cluster-manager.desired": 2,
    "ecs.tasks.cluster-manager.memory": 1024,
    "ecs.tasks.dcv-broker.cpu": 512,
    "ecs.tasks.dcv-broker.desired": 2,
    "ecs.tasks.dcv-broker.memory": 4096,
    "ecs.tasks.dcv-gateway.cpu": 256,
    "ecs.tasks.dcv-gateway.desired": 2,
    "ecs.tasks.dcv-gateway.memory": 512,
    "ecs.tasks.scheduler.cpu": 512,
    "ecs.tasks.scheduler.desired": 1,
    "ecs.tasks.scheduler.memory": 2048,
    "ecs.tasks.vdc.cpu": 256,
    "ecs.tasks.vdc.desired": 2,
    "ecs.tasks.vdc.memory": 1024,
    "global-settings.custom_tags": [],
    "global-settings.module_sets.default.cluster.module_id": "cluster",
    "global-settings.module_sets.default.cluster-manager.module_id": "cm",
    "global-settings.module_sets.default.directoryservice.module_id": "directoryservice",
    "global-settings.module_sets.default.ecs.module_id": "ecs",
    "global-settings.module_sets.default.identity-provider.module_id": "idp",
    "global-settings.module_sets.default.scheduler.module_id": "scheduler",
    "global-settings.module_sets.default.shared-storage.module_id": "storage",
    "global-settings.module_sets.default.virtual-desktop-controller.module_id": "vdc",
    "idp.cognito.provider_url": "https://identity.example.invalid",
    "metrics.provider": "cloudwatch",
    "scheduler.iam_role_arn": syntheticArn("iam", "role/synthetic-scheduler-role"),
    "scheduler.security_group_id": "sg-0123456789abcdef3",
    "storage.apps.efs.file_system_id": "fs-syntheticapps",
    "storage.apps.mount_dir": "/apps",
    "storage.apps.provider": "efs",
    "storage.data.efs.file_system_id": "fs-syntheticdata",
    "storage.data.mount_dir": "/data",
    "storage.data.provider": "efs",
    "vdc.controller.security_group_id": "sg-0123456789abcdef4",
    "vdc.controller_iam_role_arn": syntheticArn("iam", "role/synthetic-vdc-role"),
    "vdc.dcv_broker.security_group_id": "sg-0123456789abcdef5",
    "vdc.dcv_broker_role_arn": syntheticArn("iam", "role/synthetic-broker-role"),
    "vdc.dcv_connection_gateway.certificate.certificate_secret_arn": syntheticArn(
      "secretsmanager",
      "secret:synthetic-gateway-certificate",
    ),
    "vdc.dcv_connection_gateway.certificate.private_key_secret_arn": syntheticArn(
      "secretsmanager",
      "secret:synthetic-gateway-private-key",
    ),
    "vdc.dcv_connection_gateway.security_group_id": "sg-0123456789abcdef6",
    "vdc.dcv_connection_gateway_iam_role_arn": syntheticArn("iam", "role/synthetic-gateway-role"),
  };
  return new ClusterConfig(Object.entries(values).map(([key, value]) => ({ key, value })));
}

function isJson(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function record(value: unknown, description: string): Json {
  if (!isJson(value)) throw new Error(description);
  return value;
}

function resourcesOf(template: Json): Record<string, Json> {
  const resources = record(template["Resources"], "template has Resources");
  return Object.fromEntries(Object.entries(resources).map(([id, resource]) => [id, record(resource, `${id} is a resource`)]));
}

function byType(resources: Record<string, Json>, type: string): Array<[string, Json]> {
  return Object.entries(resources).filter(([, resource]) => resource["Type"] === type);
}

function findResource(resources: Record<string, Json>, type: string, predicate: (resource: Json) => boolean): Json {
  const resource = byType(resources, type).find(([, candidate]) => predicate(candidate));
  if (resource === undefined) throw new Error(`expected ${type} resource`);
  return resource[1];
}

function text(value: unknown): string {
  if (typeof value !== "string") throw new Error("expected text");
  return value;
}

function taskDefinition(resources: Record<string, Json>, role: string): Json {
  return findResource(resources, "AWS::ECS::TaskDefinition", (resource) => {
    const definitions = record(resource["Properties"], "task definition has properties")["ContainerDefinitions"];
    assert.ok(Array.isArray(definitions), "task definition has containers");
    return JSON.stringify(definitions).includes(`"IDEA_CONTAINER_ROLE","Value":"${role}"`);
  });
}

function synth(datadogEnabled = false): Json {
  const outdir = mkdtempSync(join(tmpdir(), "ideactl-ecs-"));
  workdirs.push(outdir);
  const app = new App({ context: { "aws:cdk:enable-path-metadata": true, ...vpcContext() }, outdir });
  const config = settings(datadogEnabled);
  new EcsStack({
    app,
    ctx: makeContext({
      awsRegion: REGION,
      config,
      moduleId: "ecs",
      releaseVersion: ideaVersion(),
      synthReads: SYNTH_READS,
    }),
    deploymentId: "synthetic-deployment",
    env: { account: ACCOUNT, region: REGION },
    moduleName: "ecs",
    terminationProtection: true,
  });
  return record(app.synth().getStackByName(`${CLUSTER}-ecs`).template, "synthesized template");
}

after(() => {
  for (const workdir of workdirs) rmSync(workdir, { force: true, recursive: true });
});

test("emits every planned ECS resource group without endpoint resources", () => {
  const resources = resourcesOf(synth());

  assert.equal(byType(resources, "AWS::ECS::Cluster").length, 1, "cluster");
  assert.equal(byType(resources, "AWS::ServiceDiscovery::PrivateDnsNamespace").length, 1, "service-discovery namespace");
  assert.equal(byType(resources, "AWS::EC2::LaunchTemplate").length, 1, "host launch template");
  assert.equal(byType(resources, "AWS::AutoScaling::AutoScalingGroup").length, 1, "host auto scaling group");
  assert.equal(byType(resources, "AWS::ECS::CapacityProvider").length, 1, "capacity provider");
  assert.equal(byType(resources, "AWS::ECS::TaskDefinition").length, 5, "application task definitions");
  assert.equal(byType(resources, "AWS::ECS::Service").length, 5, "application services");
  // Eleven: the gateway gets the one target group matching the session protocol setting, because
  // the desktop stack attaches only that one and a service may not name a target group with no
  // load balancer.
  assert.equal(byType(resources, "AWS::ElasticLoadBalancingV2::TargetGroup").length, 11, "IP target groups");
  assert.equal(byType(resources, "Custom::ClusterSettings").length, 1, "published settings");
  assert.equal(byType(resources, "Custom::ReleaseScaleInProtection").length, 1, "host scale-in release");
  // The stack owns the endpoints for the target groups it creates. It has to: a service may
  // not name a target group with no load balancer, and this module deploys before the modules
  // whose stacks create those rules. Seven of the eleven target groups are routed by these; the
  // gateway's is attached by the desktop stack's network load balancer.
  assert.equal(byType(resources, "Custom::EcsEndpoint").length, 6, "routed endpoints");
  assert.equal(byType(resources, "Custom::EcsDefaultEndpoint").length, 4, "listener default actions");
  const OWNED_CUSTOM_TYPES = [
    "Custom::ClusterSettings",
    "Custom::ReleaseScaleInProtection",
    "Custom::EcsEndpoint",
    "Custom::EcsDefaultEndpoint",
  ];
  assert.equal(
    Object.values(resources).filter(
      (resource) => text(resource["Type"]).startsWith("Custom::") && !OWNED_CUSTOM_TYPES.includes(text(resource["Type"])),
    ).length,
    0,
    "the ECS stack owns no other custom resources",
  );
});

test("adopts existing agent log groups with 90-day retention and preserves them on deletion", () => {
  const resources = resourcesOf(synth());
  const expectedLogGroupNames = [
    `/${CLUSTER}/cm`,
    `/${CLUSTER}/ecs/exec`,
    `/${CLUSTER}/scheduler`,
    `/${CLUSTER}/scheduler/openpbs`,
    `/${CLUSTER}/vdc/controller`,
    `/${CLUSTER}/vdc/dcv-broker`,
    `/${CLUSTER}/vdc/dcv-connection-gateway`,
  ];

  assert.equal(
    byType(resources, "AWS::Logs::LogGroup").length,
    0,
    "CloudFormation does not create agent-owned log groups that already exist",
  );

  const ensureResources = byType(resources, "AWS::CloudFormation::CustomResource");
  assert.deepEqual(
    ensureResources
      .map(([, resource]) => text(record(resource["Properties"], "ensure properties")["LogGroupName"]))
      .sort(),
    expectedLogGroupNames,
    "every agent-owned group is adopted through the create-or-adopt resource",
  );
  for (const [id, resource] of ensureResources) {
    const properties = record(resource["Properties"], `${id} properties`);
    assert.equal(Number(properties["RetentionInDays"]), 90, `${id} retention in days`);
  }

  const handler = findResource(resources, "AWS::Lambda::Function", (resource) => {
    const properties = record(resource["Properties"], "Lambda properties");
    const code = record(properties["Code"], "Lambda code");
    return typeof code["ZipFile"] === "string" && code["ZipFile"].includes("create_log_group");
  });
  const handlerCode = text(record(record(handler["Properties"], "handler properties")["Code"], "handler code")["ZipFile"]);
  assert.match(
    handlerCode,
    /except logs\.exceptions\.ResourceAlreadyExistsException:/,
    "an existing group is adopted instead of failing the deployment",
  );
  assert.match(handlerCode, /event\["RequestType"\] != "Delete"/, "Delete leaves the groups and their logs intact");
  assert.doesNotMatch(handlerCode, /delete_log_group/, "the stack never deletes adopted log groups");
});

test("keeps task definitions for circuit-breaker rollback and uses capacity providers", () => {
  const resources = resourcesOf(synth());
  for (const [id, task] of byType(resources, "AWS::ECS::TaskDefinition")) {
    assert.equal(task["DeletionPolicy"], "Retain", `${id} retained`);
    assert.equal(task["UpdateReplacePolicy"], "Retain", `${id} retained on replacement`);
    assert.deepEqual(record(task["Properties"], `${id} properties`)["RuntimePlatform"], {
      CpuArchitecture: "ARM64",
      OperatingSystemFamily: "LINUX",
    });
  }
  for (const [id, service] of byType(resources, "AWS::ECS::Service")) {
    const properties = record(service["Properties"], `${id} properties`);
    assert.ok(Array.isArray(properties["CapacityProviderStrategy"]), `${id} uses capacity provider strategy`);
    assert.equal(properties["LaunchType"], undefined, `${id} does not use launch type`);
  }
});

test("pins the scheduler deployment and health checks to the module API socket", () => {
  const resources = resourcesOf(synth());
  const scheduler = taskDefinition(resources, "scheduler");
  const schedulerProperties = record(scheduler["Properties"], "scheduler task properties");
  assert.ok(
    JSON.stringify(schedulerProperties).includes("qstat -B && curl"),
    "scheduler health check calls qstat before the module API",
  );
  assert.ok(
    JSON.stringify(schedulerProperties).includes("--unix-socket /run/idea.sock"),
    "scheduler health check uses the local socket",
  );
  assert.ok(
    JSON.stringify(schedulerProperties).includes("Scheduler.ListActiveJobs"),
    "scheduler health check names Scheduler.ListActiveJobs",
  );
  const schedulerService = findResource(resources, "AWS::ECS::Service", (resource) => {
    const deployment = record(record(resource["Properties"], "service properties")["DeploymentConfiguration"], "deployment");
    return deployment["MinimumHealthyPercent"] === 0;
  });
  const deployment = record(record(schedulerService["Properties"], "scheduler service properties")["DeploymentConfiguration"], "deployment");
  assert.equal(deployment["MinimumHealthyPercent"], 0, "scheduler minimum healthy percent");
  // The batch server holds a single-writer lock on its state directory, so a replacement task cannot
  // start while the running one holds it. A maximum of one hundred leaves no room to start first.
  assert.equal(deployment["MaximumPercent"], 100, "scheduler replacement stops before it starts");
});

test("uses the IPv4-only broker setting and blocks task access to host metadata", () => {
  const resources = resourcesOf(synth());
  const broker = taskDefinition(resources, "dcv-broker");
  assert.ok(
    JSON.stringify(broker).includes("-Djava.net.preferIPv4Stack=true"),
    "broker requires a single address family",
  );
  const launchTemplate = findResource(resources, "AWS::EC2::LaunchTemplate", () => true);
  const launchData = record(record(launchTemplate["Properties"], "launch template properties")["LaunchTemplateData"], "launch data");
  assert.equal(record(launchData["MetadataOptions"], "metadata options")["HttpTokens"], "required", "IMDSv2 required");
  assert.ok(
    JSON.stringify(launchData["UserData"]).includes("ECS_AWSVPC_BLOCK_IMDS=true"),
    "host user data blocks task access to instance metadata",
  );
});

test("never emits an account-level network interface trunking resource", () => {
  const resources = resourcesOf(synth());
  const accountSettingResources = Object.values(resources).filter((resource) => {
    const type = text(resource["Type"]);
    return type === "Custom::AWS" || type.includes("AccountSetting");
  });
  assert.equal(
    accountSettingResources.length,
    0,
    "the ECS stack does not emit account-setting resources",
  );
  assert.equal(
    Object.values(resources).filter((resource) => JSON.stringify(resource).includes("awsvpcTrunking")).length,
    0,
    "the ECS stack does not emit the account-wide trunking setting",
  );
});

test("creates the host observability daemon only when enabled", () => {
  const disabled = resourcesOf(synth(false));
  assert.equal(
    byType(disabled, "AWS::ECS::Service").length,
    5,
    "observability agent is omitted when disabled",
  );

  const enabled = resourcesOf(synth(true));
  const services = byType(enabled, "AWS::ECS::Service");
  assert.equal(services.length, 6, "one daemon service is added");
  assert.ok(
    services.some(([, service]) => record(service["Properties"], "service properties")["SchedulingStrategy"] === "DAEMON"),
    "observability service uses daemon scheduling",
  );
});

test("refuses an ECS deploy when effective account trunking is disabled", async () => {
  const errors: string[] = [];
  await assert.rejects(
    () => checkAwsvpcTrunking(
      {
        accountId: async () => ACCOUNT,
        ecsAccountSettings: {
          async listAccountSettings() {
            return [{ name: "awsvpcTrunking", value: "disabled" }];
          },
        },
        err: (line) => errors.push(line),
      },
      { awsProfile: "sample-profile", awsRegion: REGION },
    ),
    { name: "ExitWithCode" },
  );
  assert.deepEqual(errors, [
    "ECS awsvpcTrunking is not enabled for account 123456789012 in us-east-2. Without it, an m7g.large host of the planned size fits only two tasks, so placement silently starves.",
    "Run this once for the account, then repeat the deploy:",
    "aws ecs put-account-setting-default --name awsvpcTrunking --value enabled --region us-east-2 --profile sample-profile",
  ]);
});

test("allows an ECS deploy when effective account trunking is enabled", async () => {
  let reads = 0;
  await checkAwsvpcTrunking(
    {
      accountId: async () => ACCOUNT,
      ecsAccountSettings: {
        async listAccountSettings(input) {
          reads += 1;
          assert.deepEqual(input, {
            awsRegion: REGION,
            effectiveSettings: true,
            name: "awsvpcTrunking",
          });
          return [{ name: "awsvpcTrunking", value: "enabled" }];
        },
      },
      err: () => assert.fail("enabled trunking must not refuse"),
    },
    { awsRegion: REGION },
  );
  assert.equal(reads, 1);
});

test("gives an operator an audited shell into every control-plane task and no other", () => {
  const resources = resourcesOf(synth(true));

  const execGroupName = `/${CLUSTER}/ecs/exec`;
  const cluster = findResource(resources, "AWS::ECS::Cluster", () => true);
  const execConfiguration = record(
    record(record(cluster["Properties"], "cluster properties")["Configuration"], "cluster configuration")[
      "ExecuteCommandConfiguration"
    ],
    "execute command configuration",
  );
  assert.equal(execConfiguration["Logging"], "OVERRIDE", "sessions are recorded, not left to the account default");
  assert.equal(
    record(execConfiguration["LogConfiguration"], "exec log configuration")["CloudWatchLogGroupName"],
    execGroupName,
  );

  // A destination that does not exist is a feature that silently fails to log, which is the one
  // outcome that would remove the reason to prefer this path. The named group must be one this
  // stack creates or adopts, and the cluster must wait for it.
  const ensuredGroupNames = byType(resources, "AWS::CloudFormation::CustomResource").map(([, resource]) =>
    text(record(resource["Properties"], "ensure properties")["LogGroupName"]),
  );
  assert.ok(ensuredGroupNames.includes(execGroupName), "the session log group is created or adopted by this stack");
  const dependsOn = cluster["DependsOn"];
  const dependencies = Array.isArray(dependsOn) ? dependsOn : dependsOn === undefined ? [] : [dependsOn];
  assert.ok(
    dependencies.some((id) => typeof id === "string" && id.startsWith("execloggroup")),
    "the cluster waits for its session log group",
  );

  // Exactly the five control-plane services. The observability daemon is not an operator target.
  const execEnabled = byType(resources, "AWS::ECS::Service")
    .filter(([, resource]) => record(resource["Properties"], "service properties")["EnableExecuteCommand"] === true)
    .map(([id]) => id.replace(/serviceService.*$/, ""))
    .sort();
  assert.deepEqual(execEnabled, ["clustermanager", "dcvbroker", "dcvgateway", "scheduler", "vdc"]);
  const datadog = byType(resources, "AWS::ECS::Service").filter(([id]) => id.startsWith("datadogservice"));
  assert.equal(datadog.length, 1, "the observability daemon runs as its own service");
  assert.equal(
    record(datadog[0]![1]["Properties"], "datadog properties")["EnableExecuteCommand"],
    undefined,
    "the observability daemon is not an operator shell target",
  );

  // Each task role can open the session channels and write the session to that group. Without the
  // second half the session works and the recording does not.
  const roleNames = ["cluster-manager", "vdc", "scheduler", "dcv-broker", "dcv-gateway"];
  for (const role of roleNames) {
    const statements = byType(resources, "AWS::IAM::Policy")
      .filter(([id]) => id.startsWith(`${role.replace(/-/g, "")}taskrole`))
      .flatMap(([, resource]) => {
        const document = record(record(resource["Properties"], "policy properties")["PolicyDocument"], "document");
        const list = document["Statement"];
        assert.ok(Array.isArray(list), `${role} policy has statements`);
        return list.map((statement) => JSON.stringify(statement));
      });
    const joined = statements.join("\n");
    for (const action of [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
      "logs:DescribeLogGroups",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]) {
      assert.ok(joined.includes(action), `${role} task role holds ${action}`);
    }
    assert.ok(joined.includes(`log-group:${execGroupName}:*`), `${role} writes sessions to the exec group`);
  }

  // The deployment tool never holds the caller-side permission.
  assert.ok(!JSON.stringify(resources).includes("ecs:ExecuteCommand"), "no template grants ecs:ExecuteCommand");
});
