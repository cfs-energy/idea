/**
 * Synthesis harness shared by the constraint tests.
 *
 * `synthEcs` builds the ECS stack from synthetic settings only. `synthModuleStack`
 * builds a host-module stack from the captured dev27 replay fixtures, with and
 * without the container flag, so a test can compare the two templates.
 */

import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { App, Aws, Fn } from "aws-cdk-lib";

import { buildApp, type StackBuilder } from "../../src/cdk/app.ts";
import { makeContext } from "../../src/cdk/constructs/base.ts";
import { ClusterConfig } from "../../src/config/cluster-config.ts";
import { EcsStack } from "../../src/cdk/stacks/ecs.ts";
import type { SynthReads } from "../../src/cdk/synth-reads.ts";
import { ideaVersion } from "../../src/version.ts";

export type Json = Record<string, any>;

export const PKG = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
export const FIXTURES = join(PKG, "tools", "parity", "fixtures", "idea-dev27");
export const CONFIG_FILE = join(FIXTURES, "cluster-settings.json");
export const SYNTH_READS_FILE = join(FIXTURES, "synth-reads.json");
export const CONTEXT_FILE = join(FIXTURES, "cdk.context.json");

export const ACCOUNT = "123456789012";
export const ECS_CLUSTER = "idea-test1";
export const REGION = "us-east-2";
export const ECS_MODULE_ID = "ecs";
export const FIXTURE_CLUSTER = "idea-dev27";
const DEPLOYMENT_ID = "97999f4c-daaa-4813-b8ac-bd7abaedc26b";

const workdirs: string[] = [];

/** Removes every temporary synthesis directory. Call from the test file's `after`. */
export function cleanupWorkdirs(): void {
  for (const workdir of workdirs) rmSync(workdir, { force: true, recursive: true });
  workdirs.length = 0;
}

function workdir(prefix: string): string {
  const directory = mkdtempSync(join(tmpdir(), prefix));
  workdirs.push(directory);
  return directory;
}

export function readJson(path: string): Json {
  return JSON.parse(readFileSync(path, "utf8")) as Json;
}

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
    [`vpc-provider:account=${ACCOUNT}:filter.vpc-id=vpc-0123456789abcdef0:region=${REGION}:returnAsymmetricSubnets=true`]:
      {
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

function ecsSettings(datadogEnabled: boolean): ClusterConfig {
  const values: Record<string, unknown> = {
    "cluster.aws.account_id": ACCOUNT,
    "cluster.aws.dns_suffix": "amazonaws.com",
    "cluster.aws.partition": "aws",
    "cluster.aws.region": REGION,
    "cluster.cluster_name": ECS_CLUSTER,
    "cluster.cluster_s3_bucket": "sample-cluster-bucket",
    "cluster.cluster_settings_lambda_arn": syntheticArn("lambda", "function:synthetic-settings-handler"),
    // The container stack attaches its own target groups to the listeners that serve them, so it
    // reads the endpoint handler and the listeners. All four are cluster-stack outputs, published
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
    "global-settings.module_sets.default.ecs.module_id": ECS_MODULE_ID,
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

/** Synthesizes the ECS stack template. */
export function synthEcs(datadogEnabled = false): Json {
  const outdir = workdir("ideactl-oc-ecs-");
  const app = new App({ context: { "aws:cdk:enable-path-metadata": true, ...vpcContext() }, outdir });
  new EcsStack({
    app,
    ctx: makeContext({
      awsRegion: REGION,
      config: ecsSettings(datadogEnabled),
      moduleId: ECS_MODULE_ID,
      releaseVersion: ideaVersion(),
      synthReads: SYNTH_READS,
    }),
    deploymentId: "synthetic-deployment",
    env: { account: ACCOUNT, region: REGION },
    moduleName: "ecs",
    terminationProtection: true,
  });
  const template = app.synth().getStackByName(`${ECS_CLUSTER}-ecs`).template;
  return template as Json;
}

type Attribute = { S: string } | { N: string } | { BOOL: boolean } | { L: Array<{ S: string }> };

/** Copies the dev27 settings scan with the named rows set to typed attribute values. */
function configWith(overrides: Record<string, Attribute>): string {
  const scan = readJson(CONFIG_FILE);
  const remaining = new Set(Object.keys(overrides));
  for (const item of scan["Items"] as Json[]) {
    const key = item["key"]?.S as string | undefined;
    if (key !== undefined && remaining.delete(key)) item["value"] = overrides[key];
  }
  for (const key of remaining) {
    (scan["Items"] as Json[]).push({ key: { S: key }, value: overrides[key], version: { N: "1" } });
  }
  const file = join(workdir("ideactl-oc-config-"), "cluster-settings.json");
  writeFileSync(file, JSON.stringify(scan));
  return file;
}

/**
 * Synthesizes one host-module stack from the dev27 fixtures. `overrides` sets
 * settings rows, which is how the container flag and its inputs are supplied.
 */
export async function synthModuleStack(input: {
  moduleId: string;
  moduleName: string;
  stackBuilder: StackBuilder;
  overrides?: Record<string, Attribute>;
}): Promise<Json> {
  const directory = workdir(`ideactl-oc-${input.moduleId}-`);
  cpSync(CONTEXT_FILE, join(directory, "cdk.context.json"));
  cpSync(join(PKG, "cdk.json"), join(directory, "cdk.json"));
  const outdir = join(directory, `cdk.out.${input.moduleId}`);

  const previousCwd = process.cwd();
  const previousOutdir = process.env["CDK_OUTDIR"];
  const previousNag = process.env["IDEA_ADMIN_ENABLE_CDK_NAG_SCAN"];
  process.chdir(directory);
  process.env["CDK_OUTDIR"] = outdir;
  process.env["IDEA_ADMIN_ENABLE_CDK_NAG_SCAN"] = "false";
  try {
    const app = await buildApp(
      {
        clusterName: FIXTURE_CLUSTER,
        awsRegion: REGION,
        moduleId: input.moduleId,
        moduleName: input.moduleName,
        deploymentId: DEPLOYMENT_ID,
        terminationProtection: true,
        configFile: input.overrides === undefined ? CONFIG_FILE : configWith(input.overrides),
        synthReadsFile: SYNTH_READS_FILE,
      },
      { [input.moduleName]: async () => input.stackBuilder },
    );
    app.synth();
    return readJson(join(outdir, `${FIXTURE_CLUSTER}-${input.moduleId}.template.json`));
  } finally {
    process.chdir(previousCwd);
    if (previousOutdir === undefined) delete process.env["CDK_OUTDIR"];
    else process.env["CDK_OUTDIR"] = previousOutdir;
    if (previousNag === undefined) delete process.env["IDEA_ADMIN_ENABLE_CDK_NAG_SCAN"];
    else process.env["IDEA_ADMIN_ENABLE_CDK_NAG_SCAN"] = previousNag;
  }
}

export function resourcesOf(template: Json): Record<string, Json> {
  return template["Resources"] as Record<string, Json>;
}

export function byType(resources: Record<string, Json>, type: string): Array<[string, Json]> {
  return Object.entries(resources).filter(([, resource]) => resource["Type"] === type);
}

export function onlyOne(entries: Array<[string, Json]>, what: string): [string, Json] {
  if (entries.length !== 1) throw new Error(`expected exactly one ${what}, found ${entries.length}`);
  return entries[0] as [string, Json];
}
