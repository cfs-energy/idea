/**
 * Container module configuration and published settings contracts.
 */

import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { App, Aws, Fn } from "aws-cdk-lib";
import yaml from "js-yaml";

import { ClusterConfig, MODULE_METADATA } from "../../src/config/cluster-config.ts";
import { jinjaEnv, renderTemplate } from "../../src/config/jinja.ts";
import { makeContext } from "../../src/cdk/constructs/base.ts";
import { EcsStack } from "../../src/cdk/stacks/ecs.ts";
import type { SynthReads } from "../../src/cdk/synth-reads.ts";
import { loadValuesFile } from "../../src/config/values.ts";
import { ideaVersion } from "../../src/version.ts";

type Json = Record<string, unknown>;

const ACCOUNT = "123456789012";
const CLUSTER = "idea-test1";
const REGION = "us-east-2";
const templateDirectory = fileURLToPath(
  new URL("../../resources-ecs/config/templates/", import.meta.url),
);

const CONFIGURATION_KEYS = [
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

const PUBLISHED_SETTING_KEYS = [
  "capacity_provider",
  "cluster_arn",
  "cluster_name",
  "cluster-manager.service_arn",
  "cluster-manager.target_group_arns",
  "dcv-broker.service_arn",
  "dcv-broker.target_group_arns",
  // Published when the stack creates the gateway certificate itself, so the VDC
  // stack can adopt those secrets instead of creating a second certificate.
  "dcv-gateway.certificate.certificate_secret_arn",
  "dcv-gateway.certificate.private_key_secret_arn",
  "dcv-gateway.service_arn",
  "dcv-gateway.target_group_arns",
  "deployment_id",
  "image",
  "namespace_id",
  "scheduler.service_arn",
  "scheduler.target_group_arns",
  "vdc.service_arn",
  "vdc.target_group_arns",
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
    // The container stack attaches its own target groups to the listeners that serve them. All of
    // these are cluster-stack outputs, which this file already models as a dependency, so the
    // stack still reads nothing an application stack publishes.
    "cluster.cluster_endpoints_lambda_arn": syntheticArn("lambda", "function:synthetic-endpoints-handler"),
    "cluster.load_balancers.external_alb.https_listener_arn": syntheticArn("elasticloadbalancing", "listener/app/external/0123456789abcdef/0123456789abcdef"),
    "cluster.load_balancers.internal_alb.https_listener_arn": syntheticArn("elasticloadbalancing", "listener/app/internal/0123456789abcdef/0123456789abcdef"),
    "cluster.load_balancers.internal_alb.dcv_broker_client_listener_arn": syntheticArn("elasticloadbalancing", "listener/app/internal/0123456789abcdef/1123456789abcdef"),
    "cluster.load_balancers.internal_alb.dcv_broker_agent_listener_arn": syntheticArn("elasticloadbalancing", "listener/app/internal/0123456789abcdef/2123456789abcdef"),
    "cluster.load_balancers.internal_alb.dcv_broker_gateway_listener_arn": syntheticArn("elasticloadbalancing", "listener/app/internal/0123456789abcdef/3123456789abcdef"),
    "cluster-manager.endpoints.external.priority": 12,
    "cluster-manager.endpoints.external.path_patterns": ["/cluster-manager/*"],
    "cluster-manager.endpoints.internal.priority": 12,
    "cluster-manager.endpoints.internal.path_patterns": ["/cluster-manager/*"],
    "scheduler.endpoints.external.priority": 15,
    "scheduler.endpoints.external.path_patterns": ["/scheduler/*"],
    "scheduler.endpoints.internal.priority": 15,
    "scheduler.endpoints.internal.path_patterns": ["/scheduler/*"],
    "vdc.controller.endpoints.external.priority": 13,
    "vdc.controller.endpoints.external.path_patterns": ["/vdc/*"],
    "vdc.controller.endpoints.internal.priority": 13,
    "vdc.controller.endpoints.internal.path_patterns": ["/vdc/*"],
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
