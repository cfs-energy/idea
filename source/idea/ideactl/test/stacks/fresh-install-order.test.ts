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
import { ECS_HOST_SETTINGS, ECS_TASK_SETTINGS } from "../support/ecs-settings.ts";

type Json = Record<string, unknown>;

interface Dependency {
  readonly consumer: string;
  readonly producer: string;
}

interface InputResolution {
  readonly key: string;
  readonly resolution:
    | "module-task-role"
    | "module-host-security-group"
    | "vdc-self-signed-certificate";
}

const ACCOUNT = "123456789012";
const CLUSTER = "idea-test1";
const REGION = "us-east-2";
const workdirs: string[] = [];

/**
 * Complete inventory of application-stack settings removed from container-stack inputs. Each task
 * now runs with an identity and a security group its own module stack creates, so none of these
 * rows is read at container-stack synthesis.
 */
const CYCLE_INPUT_RESOLUTIONS: readonly InputResolution[] = [
  { key: "cluster-manager.iam_role_arn", resolution: "module-task-role" },
  { key: "scheduler.iam_role_arn", resolution: "module-task-role" },
  { key: "virtual-desktop-controller.controller_iam_role_arn", resolution: "module-task-role" },
  { key: "virtual-desktop-controller.dcv_broker_role_arn", resolution: "module-task-role" },
  {
    key: "virtual-desktop-controller.dcv_connection_gateway_iam_role_arn",
    resolution: "module-task-role",
  },
  { key: "cluster-manager.security_group_id", resolution: "module-host-security-group" },
  { key: "scheduler.security_group_id", resolution: "module-host-security-group" },
  {
    key: "virtual-desktop-controller.controller.security_group_id",
    resolution: "module-host-security-group",
  },
  {
    key: "virtual-desktop-controller.dcv_broker.security_group_id",
    resolution: "module-host-security-group",
  },
  {
    key: "virtual-desktop-controller.dcv_connection_gateway.security_group_id",
    resolution: "module-host-security-group",
  },
  {
    key: "virtual-desktop-controller.dcv_connection_gateway.certificate.certificate_secret_arn",
    resolution: "vdc-self-signed-certificate",
  },
  {
    key: "virtual-desktop-controller.dcv_connection_gateway.certificate.private_key_secret_arn",
    resolution: "vdc-self-signed-certificate",
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
    ...ECS_HOST_SETTINGS,
    "ecs.image": "registry.example.invalid/control-plane@sha256:abcdef",
    ...ECS_TASK_SETTINGS,
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

test("synthesizes the container stack from fresh-install state without application-stack outputs", () => {
  const resources = resourcesOf(synthFreshInstall());

  // The shared capacity, and nothing that belongs to an application. A task created here would
  // read rows that do not exist until its own module stack has run.
  assert.equal(
    Object.values(resources).filter((resource) => resource["Type"] === "AWS::ECS::TaskDefinition").length,
    0,
    "no application task definitions",
  );
  assert.equal(
    Object.values(resources).filter((resource) => resource["Type"] === "AWS::ECS::Service").length,
    0,
    "no application services",
  );
  assert.equal(
    Object.values(resources).filter((resource) => resource["Type"] === "AWS::EC2::SecurityGroup").length,
    1,
    "the container host group is the only security group this stack owns",
  );

  // No role here trusts the task service, so none of the module instance roles and none of the
  // module task roles is referenced in either spelling.
  const roleNames = Object.values(resources)
    .filter((resource) => resource["Type"] === "AWS::IAM::Role")
    .map((resource) => record(resource["Properties"], "role properties")["RoleName"]);
  // The container host role, plus the execution role of each packaged custom-resource handler.
  assert.deepEqual(
    roleNames.sort(),
    [
      `${CLUSTER}-agent-log-group-role-${REGION}`,
      `${CLUSTER}-ecs-host-role-${REGION}`,
      `${CLUSTER}-host-scale-in-release-role-${REGION}`,
    ],
    "the host role and the two handler roles",
  );
  const templateText = JSON.stringify(resources);
  for (const moduleRoleName of [
    `${CLUSTER}-cm-role-${REGION}`,
    `${CLUSTER}-scheduler-role-${REGION}`,
    `${CLUSTER}-vdc-controller-role-${REGION}`,
    `${CLUSTER}-vdc-broker-role-${REGION}`,
    `${CLUSTER}-vdc-gateway-role-${REGION}`,
    `${CLUSTER}-cm-task-role-${REGION}`,
    `${CLUSTER}-scheduler-task-role-${REGION}`,
    `${CLUSTER}-vdc-controller-task-role-${REGION}`,
  ]) {
    assert.equal(templateText.includes(moduleRoleName), false, `${moduleRoleName} belongs to a later stack`);
  }

  // The gateway certificate belongs to the stack that builds the gateway task, which is the
  // desktop stack, and that stack has created it all along. ECS neither creates nor reads it.
  const selfSignedCertificates = Object.values(resources).filter(
    (resource) => resource["Type"] === "Custom::SelfSignedCertificateConnectionGateway",
  );
  assert.equal(selfSignedCertificates.length, 0, "the container stack creates no gateway certificate");

  const settingsResource = Object.values(resources).find(
    (resource) => resource["Type"] === "Custom::ClusterSettings",
  );
  assert.notEqual(settingsResource, undefined, "the container stack publishes settings");
  const settingsText = JSON.stringify(settingsResource);
  assert.equal(
    settingsText.includes("service_arn"),
    false,
    "no service identity is published: the module stacks own their services",
  );
  assert.equal(
    settingsText.includes("target_group_arns"),
    false,
    "no target-group list is published: the module stacks own their target groups",
  );
  // The module stacks build their tasks from the architecture this stack resolved for its hosts,
  // mount the scheduler state file system beside its host group, and register the broker in its
  // namespace.
  for (const key of ["cpu_architecture", "host_security_group_id", "namespace_id", "namespace_name"]) {
    assert.ok(settingsText.includes(key), `the container stack publishes ${key}`);
  }
});
