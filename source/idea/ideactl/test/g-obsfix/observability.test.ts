/**
 * Observability continuity for the ECS cutover.
 *
 * Log group names, stream prefixes, retention, and module ids are asserted by
 * value so a rename fails. Synthetic settings only. No live account data.
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

const STREAM_PREFIX = {
  application: "application",
  openpbs: "openpbs",
  broker: "dcv-session-manager-broker",
  gateway: "dcv-connection-gateway",
  datadog: "datadog",
} as const;

/** shake256 of `idea-test1.ecs`, 4 bytes. Keep this literal so a hash change fails. */
const TARGET_GROUP_HASH = "2b157e95";

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
    "ecs.datadog.image": "123456789012.dkr.ecr.us-east-2.amazonaws.com/datadog-agent@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
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

after(() => {
  for (const workdir of workdirs) rmSync(workdir, { force: true, recursive: true });
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

test("pins preserved log group names, stream prefixes, and 90-day retention", () => {
  const resources = resourcesOf(synth());
  const logs = containerLogs(resources);

  const byRole = (role: string): ContainerLog => {
    const match = logs.find((entry) => entry.role === role);
    assert.ok(match, `container for ${role}`);
    return match;
  };

  assert.deepEqual(
    { group: byRole("cluster-manager").group, streamPrefix: byRole("cluster-manager").streamPrefix },
    { group: PRESERVED_LOG_GROUPS.clusterManager, streamPrefix: STREAM_PREFIX.application },
  );
  assert.deepEqual(
    { group: byRole("scheduler").group, streamPrefix: byRole("scheduler").streamPrefix },
    { group: PRESERVED_LOG_GROUPS.scheduler, streamPrefix: STREAM_PREFIX.application },
  );
  assert.deepEqual(
    { group: byRole("vdc").group, streamPrefix: byRole("vdc").streamPrefix },
    { group: PRESERVED_LOG_GROUPS.controller, streamPrefix: STREAM_PREFIX.application },
  );
  assert.deepEqual(
    { group: byRole("dcv-broker").group, streamPrefix: byRole("dcv-broker").streamPrefix },
    { group: PRESERVED_LOG_GROUPS.broker, streamPrefix: STREAM_PREFIX.broker },
  );
  assert.deepEqual(
    { group: byRole("dcv-gateway").group, streamPrefix: byRole("dcv-gateway").streamPrefix },
    { group: PRESERVED_LOG_GROUPS.gateway, streamPrefix: STREAM_PREFIX.gateway },
  );

  const openpbs = logs.find((entry) => entry.name === "scheduler-openpbs-logs");
  assert.ok(openpbs, "OpenPBS file-tail container");
  assert.equal(openpbs.group, PRESERVED_LOG_GROUPS.openpbs);
  assert.equal(openpbs.streamPrefix, STREAM_PREFIX.openpbs);

  const ensured = byType(resources, "AWS::CloudFormation::CustomResource");
  const names = ensured.map(([, resource]) => {
    const properties = record(resource["Properties"], "ensure properties");
    return {
      name: text(properties["LogGroupName"]),
      retention: properties["RetentionInDays"],
    };
  });
  const byName = new Map(names.map((row) => [row.name, row.retention]));
  for (const groupName of Object.values(PRESERVED_LOG_GROUPS)) {
    assert.equal(byName.get(groupName), "90", `${groupName} retention`);
  }
});

test("keeps application metric namespaces by setting IDEA_MODULE_ID to the module id", () => {
  const resources = resourcesOf(synth());
  const logs = containerLogs(resources);
  const moduleId = (role: string): string => {
    for (const [, task] of byType(resources, "AWS::ECS::TaskDefinition")) {
      const definitions = record(task["Properties"], "task properties")["ContainerDefinitions"];
      if (!Array.isArray(definitions)) continue;
      for (const definition of definitions) {
        if (!isJson(definition)) continue;
        if (environmentValue(definition, "IDEA_CONTAINER_ROLE") === role) {
          const value = environmentValue(definition, "IDEA_MODULE_ID");
          assert.ok(value, `${role} IDEA_MODULE_ID`);
          return value;
        }
      }
    }
    throw new Error(`missing ${role}`);
  };

  assert.equal(moduleId("cluster-manager"), CM_ID);
  assert.equal(moduleId("scheduler"), SCHEDULER_ID);
  assert.equal(moduleId("vdc"), VDC_ID);
  assert.equal(moduleId("dcv-broker"), VDC_ID);
  assert.equal(moduleId("dcv-gateway"), VDC_ID);

  const clusterName = (role: string): string => {
    for (const [, task] of byType(resources, "AWS::ECS::TaskDefinition")) {
      const definitions = record(task["Properties"], "task properties")["ContainerDefinitions"];
      if (!Array.isArray(definitions)) continue;
      for (const definition of definitions) {
        if (!isJson(definition)) continue;
        if (environmentValue(definition, "IDEA_CONTAINER_ROLE") === role) {
          const value = environmentValue(definition, "IDEA_CLUSTER_NAME");
          assert.ok(value, `${role} IDEA_CLUSTER_NAME`);
          return value;
        }
      }
    }
    throw new Error(`missing ${role} cluster name`);
  };

  assert.deepEqual(
    [
      `${clusterName("cluster-manager")}/${moduleId("cluster-manager")}`,
      `${clusterName("scheduler")}/${moduleId("scheduler")}`,
      `${clusterName("vdc")}/${moduleId("vdc")}/controller`,
      clusterName("dcv-broker"),
    ],
    PRESERVED_METRIC_NAMESPACE_CATALOG.map(expandCatalogName),
  );

  const applicationTails = logs.filter((entry) =>
    entry.name === "cluster-manager-application-logs" ||
    entry.name === "scheduler-application-logs" ||
    entry.name === "vdc-application-logs",
  );
  assert.deepEqual(
    applicationTails.map((entry) => entry.name).sort(),
    ["cluster-manager-application-logs", "scheduler-application-logs", "vdc-application-logs"],
  );
  assert.ok(logs.some((entry) => entry.name === "dcv-broker-file-logs"));
  assert.ok(logs.some((entry) => entry.name === "dcv-gateway-file-logs"));
});

test("pins planned target group names and does not send daemon logs to the gateway group", () => {
  const resources = resourcesOf(synth());
  const names = byType(resources, "AWS::ElasticLoadBalancingV2::TargetGroup").map(([, resource]) => {
    return text(record(resource["Properties"], "target group properties")["Name"]);
  });
  // The catalogue lists both gateway identifiers because either can be the one a cluster gets.
  // Exactly one is created, chosen by the session protocol setting, because the desktop stack
  // attaches only that one and a service may not name a target group with no load balancer. The
  // settings these resources synthesize from leave QUIC off, so it is the plain TCP identifier.
  const expected = PLANNED_TARGET_GROUP_IDENTIFIERS.filter((identifier) => identifier !== "gw-ecs-TUN");
  assert.deepEqual(
    names.sort(),
    expected.map((identifier) => `${CLUSTER}-${identifier}-${TARGET_GROUP_HASH}`).sort(),
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
