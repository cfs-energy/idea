/**
 * Observability continuity for the container cutover.
 *
 * Log group names, stream prefixes, retention, and module ids are asserted by value so a rename
 * fails. The container stack creates or adopts every agent-era group, because it deploys first and
 * owns the create-or-adopt provider; the module stacks write to those groups by name from the
 * tasks they own, so the container assertions read the module templates.
 */

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { after, test } from "node:test";
import { App, Aws, Fn } from "aws-cdk-lib";
import { load } from "js-yaml";

import { ClusterConfig } from "../../src/config/cluster-config.ts";
import { makeContext } from "../../src/cdk/constructs/base.ts";
import { EcsStack } from "../../src/cdk/stacks/ecs.ts";
import type { SynthReads } from "../../src/cdk/synth-reads.ts";
import { ideaVersion } from "../../src/version.ts";
import { ECS_HOST_SETTINGS, ECS_TASK_SETTINGS } from "../support/ecs-settings.ts";
import { requireFixtures } from "../support/fixtures.ts";
import {
  CONFIG_FILE,
  CONTEXT_FILE,
  FIXTURE_CLUSTER,
  SYNTH_READS_FILE,
  TARGET_GROUP_HASH,
  cleanupWorkdirs,
  synthContainerStacks,
  type ContainerTemplates,
} from "../support/ecs-harness.ts";

requireFixtures(
  [CONFIG_FILE, SYNTH_READS_FILE, CONTEXT_FILE],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

type Json = Record<string, unknown>;

const ACCOUNT = "123456789012";
const CLUSTER = "idea-test1";
const REGION = "us-east-2";
const CM_ID = "cm";
const SCHEDULER_ID = "scheduler";
const VDC_ID = "vdc";
const ECS_ID = "ecs";

const PRESERVED_LOG_GROUPS = {
  clusterManager: `/${CLUSTER}/${CM_ID}`,
  scheduler: `/${CLUSTER}/${SCHEDULER_ID}`,
  openpbs: `/${CLUSTER}/${SCHEDULER_ID}/openpbs`,
  controller: `/${CLUSTER}/${VDC_ID}/controller`,
  broker: `/${CLUSTER}/${VDC_ID}/dcv-broker`,
  gateway: `/${CLUSTER}/${VDC_ID}/dcv-connection-gateway`,
} as const;

const DATADOG_LOG_GROUP = `/${CLUSTER}/${ECS_ID}/datadog`;

/**
 * The same groups on the replayed fixture cluster, whose module ids are the module names. The
 * module stacks own the tasks that write to them, so their assertions use these.
 */
const MODULE_LOG_GROUPS = {
  clusterManager: `/${FIXTURE_CLUSTER}/cluster-manager`,
  scheduler: `/${FIXTURE_CLUSTER}/scheduler`,
  openpbs: `/${FIXTURE_CLUSTER}/scheduler/openpbs`,
  controller: `/${FIXTURE_CLUSTER}/vdc/controller`,
  broker: `/${FIXTURE_CLUSTER}/vdc/dcv-broker`,
  gateway: `/${FIXTURE_CLUSTER}/vdc/dcv-connection-gateway`,
} as const;

const STREAM_PREFIX = {
  application: "application",
  openpbs: "openpbs",
  broker: "dcv-session-manager-broker",
  gateway: "dcv-connection-gateway",
  datadog: "datadog",
} as const;

const PLANNED_TARGET_GROUP_IDENTIFIERS = [
  "cm-ecs-e",
  "cm-ecs-i",
  "cm-ecs-w",
  "vdc-ecs-e",
  "vdc-ecs-i",
  "sched-ecs-e",
  "sched-ecs-i",
  "brk-ecs-c",
  "brk-ecs-a",
  "brk-ecs-g",
  "gw-ecs-TN",
  "gw-ecs-TUN",
] as const;

const REGRESSION_IDS = [
  "log-group-create-collision",
  "log-group-names",
  "log-retention-90",
  "application-file-logs",
  "openpbs-logs",
  "gateway-broker-file-logs",
  "stream-prefix",
  "idea-module-id",
  "put-metric-data-iam",
  "module-asg-alarms",
  "broker-put-metric-data-without-imds",
  "production-console-handler",
] as const;

const PRESERVED_LOG_GROUP_CATALOG = [
  { name: "/{cluster}/{cm-id}", stream_prefix: "application" },
  { name: "/{cluster}/{scheduler-id}", stream_prefix: "application" },
  { name: "/{cluster}/{scheduler-id}/openpbs", stream_prefix: "openpbs" },
  { name: "/{cluster}/{vdc-id}/controller", stream_prefix: "application" },
  { name: "/{cluster}/{vdc-id}/dcv-broker", stream_prefix: "dcv-session-manager-broker" },
  { name: "/{cluster}/{vdc-id}/dcv-connection-gateway", stream_prefix: "dcv-connection-gateway" },
] as const;

const PRESERVED_METRIC_NAMESPACE_CATALOG = [
  "{cluster}/{cm-id}",
  "{cluster}/{scheduler-id}",
  "{cluster}/{vdc-id}/controller",
  "{cluster}",
] as const;

const APPLICATION_METRIC_NAMES = [
  "api_invocations_count",
  "api_invocations_duration",
  "count",
  "jobs_pending",
  "jobs_provisioned",
  "jobs_running",
  "jobs_finished",
  "jobs_pending_duration",
  "jobs_provisioning_duration",
  "jobs_running_duration",
  "jobs_total_duration",
  "nodes_ready_duration",
  "node_housekeeping_duration",
  "node_housekeeping_failed",
  "job_cache_sync_failed",
  "instance_cache_sync_failed",
  "job.count",
  "job.duration_seconds",
  "job.cost",
  "job.cost_ondemand",
  "job.savings",
  "job.cpu_efficiency",
] as const;

const PKG = dirname(fileURLToPath(import.meta.url));
const OBSERVABILITY_YAML = join(PKG, "..", "..", "resources-ecs", "observability.yml");
const workdirs: string[] = [];

/** Substitutes the synthetic cluster and module ids into a catalog placeholder. */
function expandCatalogName(name: string): string {
  return name
    .replaceAll("{cluster}", CLUSTER)
    .replaceAll("{cm-id}", CM_ID)
    .replaceAll("{scheduler-id}", SCHEDULER_ID)
    .replaceAll("{vdc-id}", VDC_ID);
}

/** Narrows a YAML row to a mapping. */
function mapping(value: unknown, label: string): Record<string, unknown> {
  if (!isRecord(value)) throw new Error(label);
  return value;
}

/** Narrows a YAML sequence to mappings. */
function mappings(value: unknown, label: string): Array<Record<string, unknown>> {
  assert.ok(Array.isArray(value), label);
  return value.map((row, index) => mapping(row, `${label} ${index}`));
}

/** Reads a required string field from a YAML row. */
function yamlString(row: Record<string, unknown>, key: string): string {
  const value = row[key];
  if (typeof value !== "string") {
    throw new Error(`${key} must be a string`);
  }
  return value;
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

function syntheticArn(service: string, resource: string): string {
  const region = service === "iam" ? "" : REGION;
  return Fn.join("", ["arn:", Aws.PARTITION, ":", service, ":", region, ":", Aws.ACCOUNT_ID, ":", resource]);
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

function settings(datadogEnabled = false): ClusterConfig {
  const values: Record<string, unknown> = {
    "cluster.aws.account_id": ACCOUNT,
    "cluster.aws.dns_suffix": "amazonaws.com",
    "cluster.aws.partition": "aws",
    "cluster.aws.region": REGION,
    "cluster.cloudwatch_logs.retention_in_days": 90,
    "cluster.cluster_name": CLUSTER,
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
    "ecs.datadog.image": "123456789012.dkr.ecr.us-east-2.amazonaws.com/datadog-agent@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ...ECS_HOST_SETTINGS,
    "ecs.image": "registry.example.invalid/control-plane@sha256:abcdef",
    ...ECS_TASK_SETTINGS,
    "global-settings.custom_tags": [],
    "global-settings.module_sets.default.cluster.module_id": "cluster",
    "global-settings.module_sets.default.cluster-manager.module_id": CM_ID,
    "global-settings.module_sets.default.directoryservice.module_id": "directoryservice",
    "global-settings.module_sets.default.ecs.module_id": ECS_ID,
    "global-settings.module_sets.default.identity-provider.module_id": "idp",
    "global-settings.module_sets.default.scheduler.module_id": SCHEDULER_ID,
    "global-settings.module_sets.default.shared-storage.module_id": "storage",
    "global-settings.module_sets.default.virtual-desktop-controller.module_id": VDC_ID,
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

function text(value: unknown): string {
  if (typeof value !== "string") throw new Error("expected text");
  return value;
}

interface ContainerLog {
  name: string;
  role: string | undefined;
  group: string;
  streamPrefix: string;
}

function environmentValue(container: Json, name: string): string | undefined {
  const environment = container["Environment"];
  if (!Array.isArray(environment)) return undefined;
  for (const entry of environment) {
    if (!isJson(entry)) continue;
    if (entry["Name"] === name && typeof entry["Value"] === "string") return entry["Value"];
  }
  return undefined;
}

function containerLogs(resources: Record<string, Json>): ContainerLog[] {
  const result: ContainerLog[] = [];
  for (const [, task] of byType(resources, "AWS::ECS::TaskDefinition")) {
    const definitions = record(task["Properties"], "task definition has properties")["ContainerDefinitions"];
    if (!Array.isArray(definitions)) continue;
    for (const definition of definitions) {
      if (!isJson(definition)) continue;
      const logging = definition["LogConfiguration"];
      if (!isJson(logging)) continue;
      const options = logging["Options"];
      if (!isJson(options)) continue;
      result.push({
        name: text(definition["Name"]),
        role: environmentValue(definition, "IDEA_CONTAINER_ROLE"),
        group: text(options["awslogs-group"]),
        streamPrefix: text(options["awslogs-stream-prefix"]),
      });
    }
  }
  return result;
}

function synth(datadogEnabled = false): Json {
  const outdir = mkdtempSync(join(tmpdir(), "ideactl-g-obsfix-"));
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

let templates: ContainerTemplates | undefined;
async function stacks(): Promise<ContainerTemplates> {
  templates ??= await synthContainerStacks();
  return templates;
}

after(() => {
  for (const workdir of workdirs) rmSync(workdir, { force: true, recursive: true });
  cleanupWorkdirs();
});

test("every regression has a verdict of fix, request, drop, or new", () => {
  const parsed: unknown = load(readFileSync(OBSERVABILITY_YAML, "utf8"));
  assert.ok(isRecord(parsed), "observability.yml is a mapping");
  assert.equal(parsed["retention_in_days"], 90);

  const groups = mappings(parsed["preserved_log_groups"], "preserved_log_groups");
  assert.deepEqual(
    groups.map((group) => ({
      name: yamlString(group, "name"),
      stream_prefix: yamlString(group, "stream_prefix"),
      verdict: yamlString(group, "verdict"),
    })),
    PRESERVED_LOG_GROUP_CATALOG.map((row) => ({ ...row, verdict: "fix" })),
  );

  const dropped = mappings(parsed["dropped"], "dropped");
  const droppedIds: string[] = [];
  for (const row of dropped) {
    assert.equal(row["verdict"], "drop");
    droppedIds.push(yamlString(row, "id"));
  }
  assert.deepEqual(droppedIds.sort(), [
    "asg-ec2-metrics",
    "asg-name-as-autoscaling-group",
    "broker-instance-id-dimension",
    "empty-dashboard",
    "host-metrics-per-module",
    "hostname-cardinality",
    "ip-stream-names",
    "lambda-log-retention",
    "prometheus-node-exporter",
    "session-manager-module-vm",
    "syslog-streams",
  ]);

  const regressions = mappings(parsed["regressions"], "regressions");
  const allowed = new Set(["fix", "request", "drop", "new"]);
  const regressionIds: string[] = [];
  for (const row of regressions) {
    const id = yamlString(row, "id");
    const verdict = yamlString(row, "verdict");
    assert.ok(allowed.has(verdict), `${id} verdict`);
    regressionIds.push(id);
  }
  assert.deepEqual(regressionIds, [...REGRESSION_IDS]);

  const metricNames = parsed["preserved_application_metric_names"];
  assert.ok(Array.isArray(metricNames), "metric names");
  assert.deepEqual(metricNames, [...APPLICATION_METRIC_NAMES]);

  const planned = parsed["planned_target_group_identifiers"];
  assert.ok(Array.isArray(planned), "planned target groups");
  assert.deepEqual(planned, [...PLANNED_TARGET_GROUP_IDENTIFIERS]);

  const namespaces = mappings(parsed["preserved_metric_namespaces"], "preserved_metric_namespaces");
  assert.deepEqual(
    namespaces.map((row) => yamlString(row, "name")),
    [...PRESERVED_METRIC_NAMESPACE_CATALOG],
  );

  const surfaces = mappings(parsed["new_surfaces"], "new_surfaces");
  const datadogSurface = surfaces.find((row) => yamlString(row, "name") === "/{cluster}/ecs/datadog");
  assert.ok(datadogSurface, "datadog surface");
  assert.equal(yamlString(datadogSurface, "stream_prefix"), STREAM_PREFIX.datadog);
});

test("does not Create AWS::Logs::LogGroup for agent-era names", () => {
  const resources = resourcesOf(synth());
  const preserved = new Set<string>(Object.values(PRESERVED_LOG_GROUPS));
  for (const [id, resource] of byType(resources, "AWS::Logs::LogGroup")) {
    const properties = record(resource["Properties"], `${id} properties`);
    const name = properties["LogGroupName"];
    if (typeof name === "string") {
      assert.equal(preserved.has(name), false, `${id} must not Create ${name}`);
    }
  }
});

test("pins preserved log group names, stream prefixes, and 90-day retention", async () => {
  const all = await stacks();
  const logs = [
    ...containerLogs(resourcesOf(all.clusterManager)),
    ...containerLogs(resourcesOf(all.scheduler)),
    ...containerLogs(resourcesOf(all.vdc)),
  ];

  const byRole = (role: string): ContainerLog => {
    const match = logs.find((entry) => entry.role === role);
    assert.ok(match, `container for ${role}`);
    return match;
  };

  assert.deepEqual(
    { group: byRole("cluster-manager").group, streamPrefix: byRole("cluster-manager").streamPrefix },
    { group: MODULE_LOG_GROUPS.clusterManager, streamPrefix: STREAM_PREFIX.application },
  );
  assert.deepEqual(
    { group: byRole("scheduler").group, streamPrefix: byRole("scheduler").streamPrefix },
    { group: MODULE_LOG_GROUPS.scheduler, streamPrefix: STREAM_PREFIX.application },
  );
  assert.deepEqual(
    { group: byRole("vdc").group, streamPrefix: byRole("vdc").streamPrefix },
    { group: MODULE_LOG_GROUPS.controller, streamPrefix: STREAM_PREFIX.application },
  );
  assert.deepEqual(
    { group: byRole("dcv-broker").group, streamPrefix: byRole("dcv-broker").streamPrefix },
    { group: MODULE_LOG_GROUPS.broker, streamPrefix: STREAM_PREFIX.broker },
  );
  assert.deepEqual(
    { group: byRole("dcv-gateway").group, streamPrefix: byRole("dcv-gateway").streamPrefix },
    { group: MODULE_LOG_GROUPS.gateway, streamPrefix: STREAM_PREFIX.gateway },
  );

  const openpbs = logs.find((entry) => entry.name === "scheduler-openpbs-logs");
  assert.ok(openpbs, "OpenPBS file-tail container");
  assert.equal(openpbs.group, MODULE_LOG_GROUPS.openpbs);
  assert.equal(openpbs.streamPrefix, STREAM_PREFIX.openpbs);

  // No module stack creates a group: every one of them is created or adopted by the container
  // stack, which deploys first and owns the provider that never deletes.
  for (const stack of ["clusterManager", "scheduler", "vdc"] as const) {
    assert.deepEqual(
      byType(resourcesOf(all[stack]), "AWS::Logs::LogGroup").map(([id]) => id),
      [],
      `${stack} creates no log group`,
    );
  }

  const ensured = byType(resourcesOf(synth()), "Custom::EnsureLogGroup");
  const byName = new Map(
    ensured.map(([, resource]) => {
      const properties = record(resource["Properties"], "ensure properties");
      return [text(properties["LogGroupName"]), properties["RetentionInDays"]];
    }),
  );
  for (const groupName of Object.values(PRESERVED_LOG_GROUPS)) {
    assert.equal(byName.get(groupName), "90", `${groupName} retention`);
  }
});

test("keeps application metric namespaces by setting IDEA_MODULE_ID to the module id", async () => {
  const all = await stacks();
  const resourcesByRole: Record<string, Record<string, Json>> = {
    "cluster-manager": resourcesOf(all.clusterManager),
    scheduler: resourcesOf(all.scheduler),
    vdc: resourcesOf(all.vdc),
    "dcv-broker": resourcesOf(all.vdc),
  };

  const environmentOf = (role: string, name: string): string => {
    for (const [, task] of byType(resourcesByRole[role] as Record<string, Json>, "AWS::ECS::TaskDefinition")) {
      const definitions = record(task["Properties"], "task properties")["ContainerDefinitions"];
      if (!Array.isArray(definitions)) continue;
      for (const definition of definitions) {
        if (!isJson(definition)) continue;
        if (environmentValue(definition, "IDEA_CONTAINER_ROLE") !== role) continue;
        const value = environmentValue(definition, name);
        assert.ok(value, `${role} ${name}`);
        return value;
      }
    }
    throw new Error(`missing ${role} ${name}`);
  };
  const moduleId = (role: string): string => environmentOf(role, "IDEA_MODULE_ID");
  const clusterName = (role: string): string => environmentOf(role, "IDEA_CLUSTER_NAME");

  assert.equal(moduleId("cluster-manager"), "cluster-manager");
  assert.equal(moduleId("scheduler"), "scheduler");
  assert.equal(moduleId("vdc"), "vdc");
  assert.equal(moduleId("dcv-broker"), "vdc");

  assert.deepEqual(
    [
      `${clusterName("cluster-manager")}/${moduleId("cluster-manager")}`,
      `${clusterName("scheduler")}/${moduleId("scheduler")}`,
      `${clusterName("vdc")}/${moduleId("vdc")}/controller`,
      clusterName("dcv-broker"),
    ],
    [
      MODULE_LOG_GROUPS.clusterManager.slice(1),
      MODULE_LOG_GROUPS.scheduler.slice(1),
      MODULE_LOG_GROUPS.controller.slice(1),
      FIXTURE_CLUSTER,
    ],
  );

  // One file-tail sidecar per application that writes `application.log`, and one each for the
  // broker and the gateway, whose files the awslogs driver cannot reach either.
  const names = [
    ...containerLogs(resourcesOf(all.clusterManager)),
    ...containerLogs(resourcesOf(all.scheduler)),
    ...containerLogs(resourcesOf(all.vdc)),
  ].map((entry) => entry.name);
  for (const expected of [
    "cluster-manager-application-logs",
    "scheduler-application-logs",
    "scheduler-openpbs-logs",
    "controller-application-logs",
    "dcv-broker-file-logs",
    "dcv-connection-gateway-file-logs",
  ]) {
    assert.ok(names.includes(expected), `${expected} sidecar`);
  }
});

test("pins planned target group names and does not send daemon logs to the gateway group", async () => {
  const all = await stacks();
  const names = (stack: "clusterManager" | "scheduler" | "vdc"): string[] =>
    byType(resourcesOf(all[stack]), "AWS::ElasticLoadBalancingV2::TargetGroup")
      .map(([, resource]) => text(record(resource["Properties"], "target group properties")["Name"]))
      .sort();

  // The catalogue lists both gateway identifiers because the session protocol setting decides
  // which one a cluster gets, and the fixture cluster has QUIC off. Each group carries the hash of
  // the module that creates it, because that is the stack that owns the listener serving it.
  assert.deepEqual(
    names("clusterManager"),
    ["cm-ecs-e", "cm-ecs-i", "cm-ecs-w"]
      .map((identifier) => `${FIXTURE_CLUSTER}-${identifier}-${TARGET_GROUP_HASH["cluster-manager"]}`)
      .sort(),
  );
  assert.deepEqual(
    names("scheduler"),
    ["sched-ecs-e", "sched-ecs-i"]
      .map((identifier) => `${FIXTURE_CLUSTER}-${identifier}-${TARGET_GROUP_HASH["scheduler"]}`)
      .sort(),
  );
  assert.deepEqual(
    names("vdc"),
    ["vdc-ecs-e", "vdc-ecs-i", "brk-ecs-c", "brk-ecs-a", "brk-ecs-g", "gw-ecs-TN"]
      .map((identifier) => `${FIXTURE_CLUSTER}-${identifier}-${TARGET_GROUP_HASH["vdc"]}`)
      .sort(),
  );
  // Every planned identifier is created by exactly one stack, and nothing else is.
  const created = new Set([...names("clusterManager"), ...names("scheduler"), ...names("vdc")]);
  for (const identifier of PLANNED_TARGET_GROUP_IDENTIFIERS) {
    const module = identifier.startsWith("cm-") ? "cluster-manager" : identifier.startsWith("sched-") ? "scheduler" : "vdc";
    const expected = `${FIXTURE_CLUSTER}-${identifier}-${TARGET_GROUP_HASH[module]}`;
    // `gw-ecs-TUN` is the QUIC spelling, which this fixture cluster does not use.
    if (identifier === "gw-ecs-TUN") {
      assert.equal(created.has(expected), false, `${identifier} is the other protocol's group`);
      continue;
    }
    assert.ok(created.has(expected), `${identifier} is created by the ${module} stack`);
  }
  const resources = resourcesOf(synth());
  assert.deepEqual(
    byType(resources, "AWS::ElasticLoadBalancingV2::TargetGroup"),
    [],
    "the container stack creates no target group",
  );

  const withDatadog = containerLogs(resourcesOf(synth(true)));
  const datadog = withDatadog.find((entry) => entry.name === "datadog-container");
  assert.ok(datadog, "datadog container");
  assert.equal(datadog.group, DATADOG_LOG_GROUP);
  assert.equal(datadog.streamPrefix, STREAM_PREFIX.datadog);

  const cluster = byType(resources, "AWS::ECS::Cluster")[0];
  assert.ok(cluster, "ecs cluster");
  const properties = record(cluster[1]["Properties"], "cluster properties");
  assert.equal(properties["ClusterName"], `${CLUSTER}-ecs`);
  const settings = properties["ClusterSettings"];
  assert.ok(Array.isArray(settings), "cluster settings");
  const insights = settings.find((entry) => isJson(entry) && entry["Name"] === "containerInsights");
  assert.ok(isJson(insights), "containerInsights");
  assert.equal(insights["Value"], "enabled");
});
