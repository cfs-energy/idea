/**
 * Fresh-install dependency checks for the container cutover.
 *
 * The fixture contains generated settings and outputs from stacks that deploy
 * before ECS. It intentionally omits every application-stack output.
 */

import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import { App, Aws, Fn } from "aws-cdk-lib";

import { EcsStack } from "../../src/cdk/stacks/ecs.ts";
import { makeContext } from "../../src/cdk/constructs/base.ts";
import { ClusterConfig, MODULE_METADATA } from "../../src/config/cluster-config.ts";
import type { SynthReads } from "../../src/cdk/synth-reads.ts";
import { ideaVersion } from "../../src/version.ts";

type Json = Record<string, unknown>;

interface Dependency {
  readonly consumer: string;
  readonly producer: string;
}

interface InputResolution {
  readonly key: string;
  readonly resolution: "ecs-task-role" | "ecs-security-group" | "ecs-self-signed-certificate";
}

const ACCOUNT = "123456789012";
const CLUSTER = "idea-test1";
const REGION = "us-east-2";
const workdirs: string[] = [];

/**
 * Complete inventory of application-stack settings removed from ECS inputs.
 */
const CYCLE_INPUT_RESOLUTIONS: readonly InputResolution[] = [
  { key: "cluster-manager.iam_role_arn", resolution: "ecs-task-role" },
  { key: "scheduler.iam_role_arn", resolution: "ecs-task-role" },
  { key: "virtual-desktop-controller.controller_iam_role_arn", resolution: "ecs-task-role" },
  { key: "virtual-desktop-controller.dcv_broker_role_arn", resolution: "ecs-task-role" },
  {
    key: "virtual-desktop-controller.dcv_connection_gateway_iam_role_arn",
    resolution: "ecs-task-role",
  },
  { key: "cluster-manager.security_group_id", resolution: "ecs-security-group" },
  { key: "scheduler.security_group_id", resolution: "ecs-security-group" },
  {
    key: "virtual-desktop-controller.controller.security_group_id",
    resolution: "ecs-security-group",
  },
  {
    key: "virtual-desktop-controller.dcv_broker.security_group_id",
    resolution: "ecs-security-group",
  },
  {
    key: "virtual-desktop-controller.dcv_connection_gateway.security_group_id",
    resolution: "ecs-security-group",
  },
  {
    key: "virtual-desktop-controller.dcv_connection_gateway.certificate.certificate_secret_arn",
    resolution: "ecs-self-signed-certificate",
  },
  {
    key: "virtual-desktop-controller.dcv_connection_gateway.certificate.private_key_secret_arn",
    resolution: "ecs-self-signed-certificate",
  },
];

/**
 * Cross-stack edges that remain after the application inputs are removed.
 */
const FRESH_INSTALL_DEPENDENCIES: readonly Dependency[] = [
  { producer: "cluster", consumer: "ecs" },
  { producer: "identity-provider", consumer: "ecs" },
  { producer: "shared-storage", consumer: "ecs" },
  { producer: "ecs", consumer: "cluster-manager" },
  { producer: "ecs", consumer: "scheduler" },
  { producer: "ecs", consumer: "virtual-desktop-controller" },
];

/** Build a partition-safe synthetic ARN without embedding a real identifier. */
function syntheticArn(service: string, resource: string): string {
  const region = service === "iam" ? "" : REGION;
  return Fn.join("", ["arn:", Aws.PARTITION, ":", service, ":", region, ":", Aws.ACCOUNT_ID, ":", resource]);
}

/** Supply the VPC lookup result without a network call. */
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

/**
 * Build generated settings plus outputs from priority 4 and earlier.
 */
function freshInstallConfig(): ClusterConfig {
  const values: Record<string, unknown> = {
    "cluster.aws.account_id": ACCOUNT,
    "cluster.aws.dns_suffix": "amazonaws.com",
    "cluster.aws.partition": "aws",
    "cluster.aws.region": REGION,
    "cluster.cluster_name": CLUSTER,
    "cluster.cluster_s3_bucket": "sample-cluster-bucket",
    "cluster.cluster_settings_lambda_arn": syntheticArn("lambda", "function:synthetic-settings-handler"),
    // The container stack attaches its own target groups to the listeners that serve them. All of
    // these are cluster-stack outputs, which this file already models as a dependency, so the
    // stack still reads nothing an application stack publishes.
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
    "cluster.iam.policies.amazon_ssm_managed_instance_core_arn": syntheticArn(
      "iam",
      "policy/synthetic-ssm-policy",
    ),
    "cluster.iam.policies.cloud_watch_agent_server_arn": syntheticArn(
      "iam",
      "policy/synthetic-cloudwatch-policy",
    ),
    "cluster.iam.roles": {},
    "cluster.load_balancers.internal_alb.load_balancer_dns_name": "internal.example.invalid",
    "cluster.network.private_subnets": ["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"],
    "cluster.network.security_groups": {},
    "cluster.network.vpc_id": "vpc-0123456789abcdef0",
    "cluster.route53.private_hosted_zone_id": "Z0123456789ABCDEF",
    "cluster.self_signed_certificate_lambda_arn": syntheticArn(
      "lambda",
      "function:synthetic-certificate-handler",
    ),
    "ecs.datadog.enabled": false,
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
    "storage.apps.efs.file_system_id": "fs-syntheticapps",
    "storage.apps.mount_dir": "/apps",
    "storage.apps.provider": "efs",
    "storage.data.efs.file_system_id": "fs-syntheticdata",
    "storage.data.mount_dir": "/data",
    "storage.data.provider": "efs",
    "vdc.dcv_connection_gateway.certificate.provided": false,
  };

  for (const input of CYCLE_INPUT_RESOLUTIONS) {
    assert.equal(input.key in values, false, `${input.key} must not be seeded`);
  }
  return new ClusterConfig(Object.entries(values).map(([key, value]) => ({ key, value })));
}

/** Reject malformed synthesized resource values. */
function record(value: unknown, description: string): Json {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(description);
  }
  return value as Json;
}

/** Return the synthesized resources keyed by logical id. */
function resourcesOf(template: Json): Record<string, Json> {
  return Object.fromEntries(
    Object.entries(record(template["Resources"], "template has Resources")).map(([id, resource]) => [
      id,
      record(resource, `${id} is a resource`),
    ]),
  );
}

/** Synthesize from the same state available at ECS priority 4.5. */
function synthFreshInstall(): Json {
  const outdir = mkdtempSync(join(tmpdir(), "ideactl-n-cycle-"));
  workdirs.push(outdir);
  const app = new App({ context: { "aws:cdk:enable-path-metadata": true, ...vpcContext() }, outdir });
  new EcsStack({
    app,
    ctx: makeContext({
      awsRegion: REGION,
      config: freshInstallConfig(),
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

const SYNTH_READS: SynthReads = {
  callerIdentity: async () => ({ account: ACCOUNT, arn: "synthetic-caller" }),
  describeDomain: async () => {
    throw new Error("fresh ECS synthesis does not read a domain");
  },
  describeListener: async () => ({}),
  describeUserPool: async () => ({}),
  listServiceLinkedRoles: async () => [],
};

after(() => {
  for (const workdir of workdirs) rmSync(workdir, { force: true, recursive: true });
});

test("orders every remaining cross-stack dependency from producer to consumer", () => {
  const priorities = new Map(MODULE_METADATA.map((module) => [module.name, module.deployment_priority]));
  assert.equal(priorities.get("ecs"), 4.5, "ECS priority is fixed between storage and applications");

  for (const dependency of FRESH_INSTALL_DEPENDENCIES) {
    const producerPriority = priorities.get(dependency.producer);
    const consumerPriority = priorities.get(dependency.consumer);
    assert.notEqual(producerPriority, undefined, `${dependency.producer} has module metadata`);
    assert.notEqual(consumerPriority, undefined, `${dependency.consumer} has module metadata`);
    assert.ok(
      Number(producerPriority) < Number(consumerPriority),
      `${dependency.producer} deploys before ${dependency.consumer}`,
    );
  }
});

test("synthesizes ECS from fresh-install state without application-stack outputs", () => {
  const resources = resourcesOf(synthFreshInstall());
  const taskSecurityGroups = Object.values(resources).filter((resource) => {
    if (resource["Type"] !== "AWS::EC2::SecurityGroup") return false;
    const description = record(resource["Properties"], "security group has properties")["GroupDescription"];
    return typeof description === "string" && description.startsWith("Security group for the ");
  });
  assert.equal(taskSecurityGroups.length, 5, "ECS owns one task ENI group per application boundary");

  const taskDefinitions = Object.values(resources).filter(
    (resource) => resource["Type"] === "AWS::ECS::TaskDefinition",
  );
  assert.equal(taskDefinitions.length, 5, "all application task definitions synthesize");
  // Each task runs as a role this stack creates. The module instance roles, which the settings above
  // do not carry and which deploy later, are not referenced in either spelling.
  const roleNames = new Map(
    Object.entries(resources)
      .filter(([, resource]) => resource["Type"] === "AWS::IAM::Role")
      .map(([id, resource]) => [id, record(resource["Properties"], `${id} properties`)["RoleName"]]),
  );
  const taskRoleNames = taskDefinitions
    .map((taskDefinition) => {
      const arn = record(taskDefinition["Properties"], "task definition properties")["TaskRoleArn"];
      const getAtt = record(arn as Json, "task role arn")["Fn::GetAtt"];
      return roleNames.get((getAtt as string[])[0] as string);
    })
    .sort();
  assert.deepEqual(
    taskRoleNames,
    [
      `${CLUSTER}-ecs-cluster-manager-task-role-${REGION}`,
      `${CLUSTER}-ecs-dcv-broker-task-role-${REGION}`,
      `${CLUSTER}-ecs-dcv-gateway-task-role-${REGION}`,
      `${CLUSTER}-ecs-scheduler-task-role-${REGION}`,
      `${CLUSTER}-ecs-vdc-task-role-${REGION}`,
    ],
    "every task runs as a task role this stack creates",
  );
  const templateText = JSON.stringify(resources);
  for (const moduleRoleName of [
    `${CLUSTER}-cm-role-${REGION}`,
    `${CLUSTER}-scheduler-role-${REGION}`,
    `${CLUSTER}-vdc-controller-role-${REGION}`,
    `${CLUSTER}-vdc-broker-role-${REGION}`,
    `${CLUSTER}-vdc-gateway-role-${REGION}`,
  ]) {
    assert.equal(templateText.includes(moduleRoleName), false, `${moduleRoleName} belongs to a later stack`);
  }

  const selfSignedCertificates = Object.values(resources).filter(
    (resource) => resource["Type"] === "Custom::SelfSignedCertificateConnectionGateway",
  );
  assert.equal(selfSignedCertificates.length, 1, "ECS creates missing self-signed gateway secrets");

  const settingsResource = Object.values(resources).find(
    (resource) => resource["Type"] === "Custom::ClusterSettings",
  );
  assert.notEqual(settingsResource, undefined, "ECS publishes settings");
  const settingsText = JSON.stringify(settingsResource);
  assert.ok(
    settingsText.includes("dcv-gateway.certificate.certificate_secret_arn"),
    "ECS publishes the generated certificate secret",
  );
  assert.ok(
    settingsText.includes("dcv-gateway.certificate.private_key_secret_arn"),
    "ECS publishes the generated private key secret",
  );
});
