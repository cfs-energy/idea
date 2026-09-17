/**
 * Synthesis harness shared by the constraint tests.
 *
 * `synthEcs` builds the ECS stack from synthetic settings only: it is the shared capacity and
 * nothing else. `synthModuleStack` builds a host-module stack from the captured dev27 replay
 * fixtures, with and without the container flag, so a test can compare the two templates. The
 * three `synth*WithEcs` helpers are the module stacks with the flag on, which is where the five
 * application services are built.
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
import { buildStack as buildClusterManagerStack } from "../../src/cdk/stacks/cluster-manager.ts";
import { buildStack as buildSchedulerStack } from "../../src/cdk/stacks/scheduler.ts";
import { buildStack as buildVdcStack } from "../../src/cdk/stacks/vdc.ts";
import type { SynthReads } from "../../src/cdk/synth-reads.ts";
import { ideaVersion } from "../../src/version.ts";
import { ECS_HOST_SETTINGS, ECS_TASK_SETTINGS } from "./ecs-settings.ts";

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

/** The capacity provider name the gateway service fixture runs its task on. */
export const ECS_CAPACITY_PROVIDER = "idea-dev27-ecs-capacity-provider";
/** The processor architecture the container stack publishes for its hosts. */
export const ECS_CPU_ARCHITECTURE = "ARM64";
/** Sizing of the gateway task, matching the values the ECS settings template carries. */
export const ECS_GATEWAY_TASK_CPU = 256;
export const ECS_GATEWAY_TASK_MEMORY = 512;
export const ECS_GATEWAY_TASK_DESIRED = 2;

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
    ...ECS_HOST_SETTINGS,
    "ecs.image": "registry.example.invalid/control-plane@sha256:abcdef",
    ...ECS_TASK_SETTINGS,
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

export type Attribute = { S: string } | { N: string } | { BOOL: boolean } | { L: Array<{ S: string }> };

/** The container image every task and sidecar runs. */
export const ECS_IMAGE = "registry.example.invalid/control-plane@sha256:abcdef";
/** The service-discovery namespace the broker registers in. */
export const ECS_NAMESPACE_ID = "ns-0123456789abcdef";
export const ECS_NAMESPACE_NAME = `${FIXTURE_CLUSTER}.ecs.local`;
/** The container host group, which the scheduler file system admits NFS from. */
export const ECS_HOST_SECURITY_GROUP_ID = "sg-0123456789abcdef7";

/**
 * Every row the container stack publishes for the module stacks, plus the task sizings from its
 * own settings template. A module stack synthesized with the flag on needs all of them.
 */
export const ECS_SHARED_CAPACITY: Record<string, Attribute> = {
  "ecs.enabled": { BOOL: true },
  "ecs.capacity_provider": { S: ECS_CAPACITY_PROVIDER },
  "ecs.cluster_name": { S: `${FIXTURE_CLUSTER}-ecs` },
  "ecs.cpu_architecture": { S: ECS_CPU_ARCHITECTURE },
  "ecs.host_security_group_id": { S: ECS_HOST_SECURITY_GROUP_ID },
  "ecs.image": { S: ECS_IMAGE },
  "ecs.namespace_id": { S: ECS_NAMESPACE_ID },
  "ecs.namespace_name": { S: ECS_NAMESPACE_NAME },
  ...Object.fromEntries(
    Object.entries(ECS_TASK_SETTINGS).map(([key, value]) => [key, { N: String(value) } as Attribute]),
  ),
};

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

/** Everything the desktop stack needs to synthesize its three task services. */
export const VDC_ECS_OVERRIDES: Record<string, Attribute> = ECS_SHARED_CAPACITY;

/** Synthesizes the cluster-manager stack with the container flag on. */
export async function synthClusterManagerWithEcs(
  overrides: Record<string, Attribute> = {},
): Promise<Json> {
  return synthModuleStack({
    moduleId: "cluster-manager",
    moduleName: "cluster-manager",
    stackBuilder: buildClusterManagerStack,
    overrides: { ...ECS_SHARED_CAPACITY, ...overrides },
  });
}

/** Synthesizes the scheduler stack with the container flag on, PBS file system included. */
export async function synthSchedulerWithEcs(overrides: Record<string, Attribute> = {}): Promise<Json> {
  return synthModuleStack({
    moduleId: "scheduler",
    moduleName: "scheduler",
    stackBuilder: buildSchedulerStack,
    overrides: { ...ECS_SHARED_CAPACITY, ...overrides },
  });
}

/** Synthesizes the desktop stack with the container flag on: controller, broker and gateway. */
export async function synthVdcWithEcs(overrides: Record<string, Attribute> = {}): Promise<Json> {
  return synthModuleStack({
    moduleId: "vdc",
    moduleName: "virtual-desktop-controller",
    stackBuilder: buildVdcStack,
    overrides: { ...ECS_SHARED_CAPACITY, ...overrides },
  });
}

/**
 * `shake256(<cluster>.<module-id>, 4)`, the uniqueness suffix of a target group name. Each module
 * stack now creates its own groups, so each carries its own module's suffix. Literals, so a change
 * to the rule fails here rather than being recomputed by the test.
 */
export const TARGET_GROUP_HASH: Readonly<Record<"cluster-manager" | "scheduler" | "vdc", string>> = {
  "cluster-manager": "76c95e5f",
  scheduler: "79a59eed",
  vdc: "e8356b3f",
};

/**
 * The same rows as plain values, for the module stack tests whose settings helper takes an untyped
 * override map rather than typed attribute values.
 */
export const ECS_SHARED_CAPACITY_VALUES: Record<string, string | number | boolean> = Object.fromEntries(
  Object.entries(ECS_SHARED_CAPACITY).map(([key, value]) => [
    key,
    "S" in value ? value.S : "N" in value ? Number(value.N) : "BOOL" in value ? value.BOOL : "",
  ]),
);

export interface ContainerTemplates {
  readonly clusterManager: Json;
  readonly scheduler: Json;
  readonly vdc: Json;
}

let containerTemplates: Promise<ContainerTemplates> | undefined;

/** The three module stacks with the container flag on, synthesized once per test file. */
export function synthContainerStacks(): Promise<ContainerTemplates> {
  containerTemplates ??= (async () => ({
    clusterManager: await synthClusterManagerWithEcs(),
    scheduler: await synthSchedulerWithEcs(),
    vdc: await synthVdcWithEcs(),
  }))();
  return containerTemplates;
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
