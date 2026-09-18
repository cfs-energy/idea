/**
 * Production security checks for the container control plane.
 *
 * The shared-capacity cases synthesize the container stack from synthetic settings. Every task
 * case replays the captured module settings, because each task is built by the module stack that
 * publishes the rows its process reads at boot.
 */

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import { App, Aws, Fn } from "aws-cdk-lib";

import { EcsStack } from "../../src/cdk/stacks/ecs.ts";
import { makeContext } from "../../src/cdk/constructs/base.ts";
import { ClusterConfig } from "../../src/config/cluster-config.ts";
import type { SynthReads } from "../../src/cdk/synth-reads.ts";
import { ideaVersion } from "../../src/version.ts";
import { settingsLookup } from "../../tools/parity/intended-drift.ts";
import { requireCapture } from "../support/fixtures.ts";
import { ECS_HOST_SETTINGS, ECS_TASK_SETTINGS } from "../support/ecs-settings.ts";
import {
  CONFIG_FILE,
  CONTEXT_FILE,
  ECS_HOST_SECURITY_GROUP_ID,
  FIXTURE_CLUSTER,
  REGION as FIXTURE_REGION,
  SYNTH_READS_FILE,
  cleanupWorkdirs,
  synthClusterManagerWithEcs,
  synthContainerStacks,
  synthSchedulerWithEcs,
  synthVdcWithEcs,
  type ContainerTemplates,
} from "../support/ecs-harness.ts";

requireCapture(
  [CONFIG_FILE, SYNTH_READS_FILE, CONTEXT_FILE],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

type Json = Record<string, unknown>;

interface FixtureOptions {
  datadogEnabled?: boolean;
  datadogImage?: string;
  /** The desktop module's QUIC transport, which both real clusters have on and development has off. */
  quicSupported?: boolean;
}

const ACCOUNT = "123456789012";
const CLUSTER = "idea-test1";
const REGION = "us-east-2";
/** The module group ids the settings still carry and this stack must not read. */
const MODULE_SECURITY_GROUP_IDS = [
  "sg-0123456789abcdef2",
  "sg-0123456789abcdef3",
  "sg-0123456789abcdef4",
  "sg-0123456789abcdef5",
  "sg-0123456789abcdef6",
];
const APPLICATION_ROLES = ["cluster-manager", "scheduler", "vdc", "dcv-broker"];
/** Logical ids in the desktop template: the gateway task identities, and the host roles. */
const GATEWAY_TASK_ROLES = ["dcvconnectiongatewaytaskrole083CEE07", "dcvconnectiongatewaytaskexecutionroleD6FA88E6"];
const DESKTOP_HOST_ROLES = ["vdccontrollerroleB8A27FF1", "vdcbrokerrole21DFD357", "vdcgatewayroleDD58223F"];
const GATEWAY_TASK_DEFINITION = "dcvconnectiongatewaytaskdefinitionCA8DCE2B";
/** The certificate pair the gateway execution role reads, as the captured settings publish it. */
const GATEWAY_CERTIFICATE_ROWS = [
  "vdc.dcv_connection_gateway.certificate.certificate_secret_arn",
  "vdc.dcv_connection_gateway.certificate.private_key_secret_arn",
] as const;
const CLUSTER_PREFIX_LIST = "pl-0123456789abcdef0";
const OPERATOR_PREFIX_LIST = "pl-0fedcba9876543210";
const DATADOG_DIGEST =
  `123456789012.dkr.ecr.us-east-2.amazonaws.com/observability-agent@sha256:${"a".repeat(64)}`;
const REQUEST_FILE = new URL("../../docs/port/requests/security-fix.md", import.meta.url);
const workdirs: string[] = [];

const SYNTH_READS: SynthReads = {
  callerIdentity: async () => ({ account: ACCOUNT, arn: "synthetic-caller" }),
  describeDomain: async () => {
    throw new Error("Synthetic ECS synthesis does not read domain settings");
  },
  describeListener: async () => ({}),
  describeUserPool: async () => ({}),
  listServiceLinkedRoles: async () => [],
};

/** Builds a partition-aware synthetic ARN without embedding a deployed ARN. */
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

/** Supplies the cached VPC lookup used by the synthetic stack. */
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

/** Creates complete synthetic settings for ECS stack synthesis. */
function settings(options: FixtureOptions = {}): ClusterConfig {
  const clusterManagerRole = syntheticArn("iam", "role/synthetic-cluster-manager-role");
  const values: Record<string, unknown> = {
    "cluster.aws.account_id": ACCOUNT,
    "cluster.aws.dns_suffix": "amazonaws.com",
    "cluster.aws.partition": "aws",
    "cluster.aws.region": REGION,
    "cluster.cluster_name": CLUSTER,
    "cluster.cluster_s3_bucket": "sample-cluster-bucket",
    "cluster.cluster_settings_lambda_arn": syntheticArn(
      "lambda",
      "function:synthetic-settings-handler",
    ),
    "cluster.iam.policies.amazon_ssm_managed_instance_core_arn": syntheticArn(
      "iam",
      "policy/synthetic-ssm-policy",
    ),
    "cluster.iam.policies.cloud_watch_agent_server_arn": syntheticArn(
      "iam",
      "policy/synthetic-cloudwatch-policy",
    ),
    "cluster.iam.roles": {},
    "cluster.load_balancers.internal_alb.load_balancer_dns_name":
      "internal.example.invalid",
    "cluster.network.private_subnets": [
      "subnet-0123456789abcdef0",
      "subnet-0123456789abcdef1",
    ],
    "cluster.network.security_groups.external-load-balancer":
      "sg-0123456789abcdef0",
    "cluster.network.security_groups.internal-load-balancer":
      "sg-0123456789abcdef1",
    "cluster.network.vpc_id": "vpc-0123456789abcdef0",
    "cluster.network.cluster_prefix_list_id": CLUSTER_PREFIX_LIST,
    "cluster.network.prefix_list_ids": [OPERATOR_PREFIX_LIST],
    "cluster.route53.private_hosted_zone_id": "Z0123456789ABCDEF",
    "cm.iam_role_arn": clusterManagerRole,
    "cm.security_group_id": "sg-0123456789abcdef2",
    "directoryservice.directory_id": "d-0000000001",
    "directoryservice.provider": "aws_managed_activedirectory",
    "ecs.datadog.api_key_secret_arn": syntheticArn(
      "secretsmanager",
      "secret:synthetic-observability-secret",
    ),
    // A cluster that has this stack always carries the flag, and the module policy templates gate
    // container-only grants on it.
    "ecs.enabled": true,
    "ecs.datadog.enabled": options.datadogEnabled ?? false,
    "ecs.datadog.image": options.datadogImage ?? DATADOG_DIGEST,
    ...ECS_HOST_SETTINGS,
    "ecs.image":
      "registry.example.invalid/control-plane@sha256:abcdef",
    ...ECS_TASK_SETTINGS,
    "global-settings.custom_tags": [],
    "global-settings.module_sets.default.cluster.module_id": "cluster",
    "global-settings.module_sets.default.cluster-manager.module_id": "cm",
    "global-settings.module_sets.default.directoryservice.module_id": "directoryservice",
    "global-settings.module_sets.default.ecs.module_id": "ecs",
    "global-settings.module_sets.default.identity-provider.module_id": "idp",
    "global-settings.module_sets.default.scheduler.module_id": "scheduler",
    "global-settings.module_sets.default.shared-storage.module_id": "storage",
    "global-settings.module_sets.default.virtual-desktop-controller.module_id":
      "vdc",
    "idp.cognito.provider_url": "https://identity.example.invalid",
    "metrics.provider": "cloudwatch",
    "scheduler.iam_role_arn": syntheticArn(
      "iam",
      "role/synthetic-scheduler-role",
    ),
    "scheduler.security_group_id": "sg-0123456789abcdef3",
    "storage.apps.efs.file_system_id": "fs-syntheticapps",
    "storage.apps.mount_dir": "/apps",
    "storage.apps.provider": "efs",
    "storage.data.efs.file_system_id": "fs-syntheticdata",
    "storage.data.mount_dir": "/data",
    "storage.data.provider": "efs",
    "vdc.controller.security_group_id": "sg-0123456789abcdef4",
    "vdc.controller_iam_role_arn": syntheticArn(
      "iam",
      "role/synthetic-vdc-role",
    ),
    "vdc.dcv_broker.security_group_id": "sg-0123456789abcdef5",
    "vdc.dcv_session.quic_support": options.quicSupported ?? false,
    "vdc.dcv_broker_role_arn": syntheticArn("iam", "role/synthetic-broker-role"),
    "vdc.dcv_connection_gateway.certificate.certificate_secret_arn":
      syntheticArn(
        "secretsmanager",
        "secret:synthetic-gateway-certificate",
      ),
    "vdc.dcv_connection_gateway.certificate.private_key_secret_arn":
      syntheticArn(
        "secretsmanager",
        "secret:synthetic-gateway-private-key",
      ),
    "vdc.dcv_connection_gateway.security_group_id":
      "sg-0123456789abcdef6",
    "vdc.dcv_connection_gateway_iam_role_arn": syntheticArn(
      "iam",
      "role/synthetic-gateway-role",
    ),
  };
  return new ClusterConfig(
    Object.entries(values).map(([key, value]) => ({ key, value })),
  );
}

/** Returns true only for JSON object values. */
function isJson(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Validates and returns a JSON object. */
function record(value: unknown, description: string): Json {
  if (!isJson(value)) throw new Error(description);
  return value;
}

/** Returns all synthesized resources keyed by logical ID. */
function resourcesOf(template: Json): Record<string, Json> {
  const resources = record(template["Resources"], "Template has Resources");
  return Object.fromEntries(
    Object.entries(resources).map(([id, resource]) => [
      id,
      record(resource, `${id} is a resource`),
    ]),
  );
}

/** Returns resources of one CloudFormation type. */
function byType(
  resources: Record<string, Json>,
  type: string,
): Array<[string, Json]> {
  return Object.entries(resources).filter(
    ([, resource]) => resource["Type"] === type,
  );
}

/** Returns the one resource of a type that passes a predicate. */
function findResource(
  resources: Record<string, Json>,
  type: string,
  predicate: (resource: Json, id: string) => boolean,
): [string, Json] {
  const resource = byType(resources, type).find(([id, candidate]) =>
    predicate(candidate, id),
  );
  if (resource === undefined) throw new Error(`Expected ${type} resource`);
  return resource;
}

/** Returns a task definition identified by its application role environment. */
function taskDefinition(
  resources: Record<string, Json>,
  role: string,
): [string, Json] {
  return findResource(
    resources,
    "AWS::ECS::TaskDefinition",
    (resource) =>
      JSON.stringify(resource).includes(
        `"Name":"IDEA_CONTAINER_ROLE","Value":"${role}"`,
      ),
  );
}

/** Reads a property from a resource after validating its shape. */
function properties(resource: Json, description: string): Json {
  return record(resource["Properties"], `${description} has Properties`);
}

/** Synthesizes the ECS template without network or account calls. */
function synth(options: FixtureOptions = {}): Json {
  const outdir = mkdtempSync(join(tmpdir(), "ideactl-i-security-"));
  workdirs.push(outdir);
  const app = new App({
    context: {
      "aws:cdk:enable-path-metadata": true,
      ...vpcContext(),
    },
    outdir,
  });
  new EcsStack({
    app,
    ctx: makeContext({
      awsRegion: REGION,
      config: settings(options),
      moduleId: "ecs",
      releaseVersion: ideaVersion(),
      synthReads: SYNTH_READS,
    }),
    deploymentId: "synthetic-deployment",
    env: { account: ACCOUNT, region: REGION },
    moduleName: "ecs",
    terminationProtection: true,
  });
  return record(
    app.synth().getStackByName(`${CLUSTER}-ecs`).template,
    "Synthesized template",
  );
}

/** Extracts a Ref logical ID from a CloudFormation value. */
function refId(value: unknown): string | undefined {
  if (!isJson(value)) return undefined;
  return typeof value["Ref"] === "string" ? value["Ref"] : undefined;
}

/** Extracts a resource logical ID from Ref or Fn::GetAtt. */
function resourceId(value: unknown): string | undefined {
  const referenced = refId(value);
  if (referenced !== undefined) return referenced;
  if (!isJson(value)) return undefined;
  const getAtt = value["Fn::GetAtt"];
  return Array.isArray(getAtt) && typeof getAtt[0] === "string"
    ? getAtt[0]
    : undefined;
}

/** Reads the task definition containers as JSON objects. */
function containers(task: Json): Json[] {
  const value = properties(task, "Task definition")["ContainerDefinitions"];
  if (!Array.isArray(value)) {
    throw new Error("Task definition has no ContainerDefinitions");
  }
  return value.map((container) => record(container, "Container is an object"));
}

/** Reads the security hardening request. */
function requestText(): string {
  return readFileSync(REQUEST_FILE, "utf8");
}

/** The three module stacks with the container flag on, synthesized once for the task cases. */
let moduleTemplates: Promise<ContainerTemplates> | undefined;
function moduleStacks(): Promise<ContainerTemplates> {
  moduleTemplates ??= synthContainerStacks();
  return moduleTemplates;
}

function desktop(): Promise<Json> {
  return moduleStacks().then((all) => all.vdc as Json);
}

/** Every task role, execution role included, by physical name and by module stack. */
const TASK_IDENTITY_NAMES: Record<keyof ContainerTemplates, string[]> = {
  clusterManager: [
    `${FIXTURE_CLUSTER}-cluster-manager-task-execution-role-${FIXTURE_REGION}`,
    `${FIXTURE_CLUSTER}-cluster-manager-task-role-${FIXTURE_REGION}`,
  ],
  scheduler: [
    `${FIXTURE_CLUSTER}-scheduler-task-execution-role-${FIXTURE_REGION}`,
    `${FIXTURE_CLUSTER}-scheduler-task-role-${FIXTURE_REGION}`,
  ],
  vdc: [
    `${FIXTURE_CLUSTER}-vdc-broker-task-execution-role-${FIXTURE_REGION}`,
    `${FIXTURE_CLUSTER}-vdc-broker-task-role-${FIXTURE_REGION}`,
    `${FIXTURE_CLUSTER}-vdc-controller-task-execution-role-${FIXTURE_REGION}`,
    `${FIXTURE_CLUSTER}-vdc-controller-task-role-${FIXTURE_REGION}`,
    `${FIXTURE_CLUSTER}-vdc-gateway-task-execution-role-${FIXTURE_REGION}`,
    `${FIXTURE_CLUSTER}-vdc-gateway-task-role-${FIXTURE_REGION}`,
  ],
};

/**
 * Requires that only the task identities trust the task service, in every module stack.
 *
 * Each module stack now builds both: the instance roles its hosts run on until they retire, and
 * the task roles its tasks run on. Naming the trusting set rather than counting it is what makes a
 * host role that gains the trust fail here.
 */
async function assertModuleInstanceRolesOmitTaskTrust(): Promise<void> {
  const all = await moduleStacks();
  for (const stack of ["clusterManager", "scheduler", "vdc"] as const) {
    const resources = resourcesOf(all[stack] as Json);
    const trusting = byType(resources, "AWS::IAM::Role")
      .filter(([, role]) =>
        JSON.stringify(properties(role, "role")["AssumeRolePolicyDocument"]).includes("ecs-tasks."),
      )
      .map(([, role]) => properties(role, "role")["RoleName"] as string)
      .sort();
    assert.deepEqual(
      trusting,
      TASK_IDENTITY_NAMES[stack],
      `only the task identities trust the task service in ${stack}`,
    );
  }
  const desktopResources = resourcesOf(await desktop());
  for (const id of DESKTOP_HOST_ROLES) {
    assert.ok(desktopResources[id] !== undefined, `${id} is still an instance role of this stack`);
  }
}

after(() => {
  for (const workdir of workdirs) {
    rmSync(workdir, { force: true, recursive: true });
  }
  cleanupWorkdirs();
});

test("1. pins metadata isolation on every replacement host", () => {
  const resources = resourcesOf(synth());
  const [, launchTemplate] = findResource(
    resources,
    "AWS::EC2::LaunchTemplate",
    () => true,
  );
  const launchData = record(
    properties(launchTemplate, "Launch template")["LaunchTemplateData"],
    "Launch template has LaunchTemplateData",
  );
  assert.deepEqual(launchData["MetadataOptions"], {
    HttpPutResponseHopLimit: 1,
    HttpTokens: "required",
  });
  assert.ok(
    JSON.stringify(launchData["UserData"]).includes(
      "ECS_AWSVPC_BLOCK_IMDS=true",
    ),
  );

  const request = requestText();
  const coverageSentence = request.match(
    /The check must cover [\s\S]*?after each service reaches its desired/,
  )?.[0];
  if (coverageSentence === undefined) {
    throw new Error("Startup proof names the roles it covers");
  }
  for (const role of [
    "cluster-manager",
    "scheduler",
    "VDC controller",
    "DCV broker",
    "DCV gateway",
  ]) {
    assert.ok(coverageSentence.includes(role), `Startup proof names ${role}`);
  }
  assert.ok(
    request.includes("Fail application startup if instance metadata is reachable"),
  );
});

/** A task definition of one container role, in one module template. */
function moduleTaskDefinition(resources: Record<string, Json>, role: string): [string, Json] {
  return taskDefinition(resources, role);
}

/** Every service in a template, with the security groups its ENI carries, by group logical id. */
function serviceSecurityGroupIds(resources: Record<string, Json>): Map<string, string[]> {
  const byService = new Map<string, string[]>();
  for (const [id, service] of byType(resources, "AWS::ECS::Service")) {
    const network = properties(service, "ECS service")["NetworkConfiguration"];
    if (!isJson(network)) continue;
    const awsvpc = network["AwsvpcConfiguration"];
    if (!isJson(awsvpc)) continue;
    const groups = awsvpc["SecurityGroups"];
    byService.set(
      id,
      (Array.isArray(groups) ? groups : []).flatMap((entry) => {
        const referenced = resourceId(entry);
        return referenced === undefined ? [] : [referenced];
      }),
    );
  }
  return byService;
}

/** Every security group logical id a launch template or a bare instance puts on a host ENI. */
function hostSecurityGroupIds(resources: Record<string, Json>): Set<string> {
  const ids = new Set<string>();
  for (const [, launchTemplate] of byType(resources, "AWS::EC2::LaunchTemplate")) {
    const data = properties(launchTemplate, "Launch template")["LaunchTemplateData"];
    if (!isJson(data)) continue;
    for (const entry of Array.isArray(data["SecurityGroupIds"]) ? data["SecurityGroupIds"] : []) {
      const referenced = resourceId(entry);
      if (referenced !== undefined) ids.add(referenced);
    }
  }
  for (const [, instance] of byType(resources, "AWS::EC2::Instance")) {
    const interfaces = properties(instance, "Instance")["NetworkInterfaces"];
    for (const entry of Array.isArray(interfaces) ? interfaces : []) {
      if (!isJson(entry)) continue;
      for (const group of Array.isArray(entry["GroupSet"]) ? entry["GroupSet"] : []) {
        const referenced = resourceId(group);
        if (referenced !== undefined) ids.add(referenced);
      }
    }
  }
  return ids;
}

test("2. runs each task in the host security group of the component it replaces", async () => {
  // The task does not copy the host group's rules, it joins the group. Under
  // `ecs.retain_existing_hosts` the host and the task are on it at the same time, which is what
  // makes "the task holds the position the host holds" a property of the template rather than a
  // rule-by-rule transcription that can drift.
  const retained: Array<[string, Json]> = [
    ["cluster-manager", (await synthClusterManagerWithEcs({ "ecs.retain_existing_hosts": { BOOL: true } })) as Json],
    ["scheduler", (await synthSchedulerWithEcs({ "ecs.retain_existing_hosts": { BOOL: true } })) as Json],
    ["vdc", (await synthVdcWithEcs({ "ecs.retain_existing_hosts": { BOOL: true } })) as Json],
  ];
  for (const [module, template] of retained) {
    const resources = resourcesOf(template);
    const hostGroups = hostSecurityGroupIds(resources);
    assert.ok(hostGroups.size > 0, `${module} still has hosts to compare against`);
    const services = serviceSecurityGroupIds(resources);
    assert.ok(services.size > 0, `${module} owns a service`);
    for (const [serviceId, groups] of services) {
      assert.equal(groups.length, 1, `${module}.${serviceId} task ENI carries one group`);
      assert.ok(
        hostGroups.has(groups[0] as string),
        `${module}.${serviceId} runs in a group its own hosts use, not a copy of one`,
      );
    }
  }

  // The container host group takes no application traffic at all: it is the instance ENI's group,
  // and every listener reaches a task ENI instead.
  const container = resourcesOf(synth());
  const [hostGroupId, hostGroup] = findResource(
    container,
    "AWS::EC2::SecurityGroup",
    (resource) => properties(resource, "Security group")["GroupDescription"] === "Security group for ECS container hosts",
  );
  assert.equal(properties(hostGroup, "Host security group")["SecurityGroupIngress"], undefined);
  for (const [, ingress] of byType(container, "AWS::EC2::SecurityGroupIngress")) {
    const rule = properties(ingress, "Ingress");
    if (resourceId(rule["GroupId"]) !== hostGroupId) continue;
    assert.fail("Host group must not receive application listener traffic");
  }
  assert.equal(
    byType(container, "AWS::EC2::SecurityGroup").length,
    1,
    "the container stack owns one security group: the hosts'",
  );

  // And it reads no module group id, so nothing here depends on a stack that deploys later.
  const containerText = JSON.stringify(container);
  for (const moduleGroupId of MODULE_SECURITY_GROUP_IDS) {
    assert.equal(
      containerText.includes(moduleGroupId),
      false,
      `${moduleGroupId} is a module group this stack does not read`,
    );
  }
});

test("3. keeps PBS state on a scheduler-only encrypted EFS access point", async () => {
  const resources = resourcesOf((await moduleStacks()).scheduler as Json);
  const [fileSystemId, fileSystem] = findResource(resources, "AWS::EFS::FileSystem", () => true);
  assert.equal(fileSystem["DeletionPolicy"], "Retain");
  assert.equal(fileSystem["UpdateReplacePolicy"], "Retain");
  assert.equal(properties(fileSystem, "PBS file system")["Encrypted"], true);

  const policy = record(
    properties(fileSystem, "PBS file system")["FileSystemPolicy"],
    "PBS file system has FileSystemPolicy",
  );
  const statements = policy["Statement"];
  assert.ok(Array.isArray(statements));
  assert.ok(
    statements.some((statement) =>
      JSON.stringify(statement).includes("\"ArnNotEquals\":{\"aws:PrincipalArn\""),
    ),
    "Policy denies every non-scheduler principal",
  );
  // The condition matches the shape of an access point rather than naming one: naming it is a
  // reference from the file system's own policy to the access point, and the access point already
  // refers to the file system, which CloudFormation refuses as a circular dependency. A mount can
  // only present an access point of the file system it is mounting, and this file system has one.
  // A negated string condition is true when the key is absent, so a mount presenting no access
  // point is denied too.
  assert.ok(
    statements.some((statement) =>
      JSON.stringify(statement).includes("\"StringNotLike\":{\"elasticfilesystem:AccessPointArn\""),
    ),
    "Policy denies mounts outside the scheduler access point",
  );
  const schedulerTaskRole = findResource(
    resources,
    "AWS::IAM::Role",
    (resource) =>
      properties(resource, "Role")["RoleName"] === `${FIXTURE_CLUSTER}-scheduler-task-role-${FIXTURE_REGION}`,
  )[0];
  // The allow names the role and requires a mount target. Restricting it to the access point is
  // the deny asserted above, which is where it has to live: a condition naming the access point
  // in the file system's own policy is a resource cycle CloudFormation refuses.
  assert.ok(
    statements.some((statement) => {
      const record_ = record(statement, "Statement");
      if (record_["Effect"] !== "Allow") return false;
      const principal = record(record_["Principal"], "Principal");
      return (
        resourceId(principal["AWS"]) === schedulerTaskRole &&
        JSON.stringify(record_["Condition"]).includes("\"elasticfilesystem:AccessedViaMountTarget\"")
      );
    }),
    "Policy allows the scheduler task role, and only through a mount target",
  );

  const [, accessPoint] = findResource(resources, "AWS::EFS::AccessPoint", () => true);
  assert.equal(accessPoint["DeletionPolicy"], "Retain");
  assert.equal(accessPoint["UpdateReplacePolicy"], "Retain");
  const accessPointProperties = properties(accessPoint, "PBS access point");
  assert.deepEqual(accessPointProperties["PosixUser"], { Gid: "0", Uid: "0" });
  assert.ok(JSON.stringify(accessPointProperties["RootDirectory"]).includes("\"Permissions\":\"0700\""));

  const [schedulerId, scheduler] = moduleTaskDefinition(resources, "scheduler");
  const schedulerProperties = properties(scheduler, "Scheduler task");
  const volumes = schedulerProperties["Volumes"];
  assert.ok(Array.isArray(volumes));
  const pbsVolume = volumes
    .map((volume) => record(volume, "Volume is an object"))
    .find((volume) => volume["Name"] === "scheduler-pbs");
  assert.notEqual(pbsVolume, undefined);
  const efsConfiguration = record(
    pbsVolume?.["EFSVolumeConfiguration"],
    "PBS volume has EFSVolumeConfiguration",
  );
  assert.equal(refId(efsConfiguration["FilesystemId"]), fileSystemId);
  assert.equal(efsConfiguration["TransitEncryption"], "ENABLED");
  assert.equal(
    record(efsConfiguration["AuthorizationConfig"], "PBS volume has AuthorizationConfig")["IAM"],
    "ENABLED",
  );
  assert.ok(JSON.stringify(containers(scheduler)).includes("\"Name\":\"PBS_HOME\",\"Value\":\"/var/spool/pbs\""));
  assert.ok(JSON.stringify(containers(scheduler)).includes("\"ContainerPath\":\"/var/spool/pbs\""));

  // The mount can be made from the task interface or from the container host interface, depending
  // on the container agent, and it fails silently from the wrong one. The task group belongs to
  // this stack and the host group's id comes from the row the container stack published; the file
  // system policy above is what limits access to the scheduler task role.
  const groupNameOf = (value: unknown): string | undefined => {
    const id = resourceId(value);
    const group = id === undefined ? undefined : resources[id];
    return group?.["Type"] === "AWS::EC2::SecurityGroup"
      ? (properties(group, "Group")["GroupName"] as string)
      : undefined;
  };
  const fileSystemGroup = findResource(
    resources,
    "AWS::EC2::SecurityGroup",
    (resource) =>
      properties(resource, "Security group")["GroupDescription"] ===
      "Allows NFS only from the scheduler task and its host",
  )[0];
  const nfsPeers = byType(resources, "AWS::EC2::SecurityGroupIngress")
    .map(([, resource]) => properties(resource, "Security group ingress"))
    .filter((rule) => resourceId(rule["GroupId"]) === fileSystemGroup)
    .map((rule) => ({
      IpProtocol: rule["IpProtocol"],
      FromPort: rule["FromPort"],
      ToPort: rule["ToPort"],
      Source: groupNameOf(rule["SourceSecurityGroupId"]) ?? rule["SourceSecurityGroupId"],
    }))
    .sort((left, right) => String(left.Source).localeCompare(String(right.Source)));
  assert.deepEqual(nfsPeers, [
    {
      IpProtocol: "tcp",
      FromPort: 2049,
      ToPort: 2049,
      Source: `${FIXTURE_CLUSTER}-scheduler-security-group`,
    },
    {
      IpProtocol: "tcp",
      FromPort: 2049,
      ToPort: 2049,
      Source: ECS_HOST_SECURITY_GROUP_ID,
    },
  ]);

  for (const [id, task] of byType(resources, "AWS::ECS::TaskDefinition")) {
    if (id === schedulerId) continue;
    assert.ok(!JSON.stringify(task).includes("scheduler-pbs"), `${id} cannot mount PBS state`);
  }
});

/** Container role to the module stack that owns it, and the physical name of its task role. */
const TASK_ROLES: ReadonlyArray<{ role: string; stack: keyof ContainerTemplates; name: string }> = [
  { role: "cluster-manager", stack: "clusterManager", name: `${FIXTURE_CLUSTER}-cluster-manager-task-role-${FIXTURE_REGION}` },
  { role: "scheduler", stack: "scheduler", name: `${FIXTURE_CLUSTER}-scheduler-task-role-${FIXTURE_REGION}` },
  { role: "vdc", stack: "vdc", name: `${FIXTURE_CLUSTER}-vdc-controller-task-role-${FIXTURE_REGION}` },
  { role: "dcv-broker", stack: "vdc", name: `${FIXTURE_CLUSTER}-vdc-broker-task-role-${FIXTURE_REGION}` },
];

/**
 * One action per module policy template that no other module's template carries, read from the
 * templates under `resources/policies`. The gateway template's actions are all present in another
 * template, so it is pinned by an action it has together with the absence of the other four markers.
 */
const POLICY_MARKERS: Record<string, string> = {
  "cluster-manager": "cognito-idp:*",
  vdc: "application-autoscaling:PutScalingPolicy",
  scheduler: "cloudformation:CreateStack",
  "dcv-broker": "sns:Publish",
};
/** The same, for the gateway template the desktop stack renders onto its task role. */
const GATEWAY_POLICY_MARKER = "fsx:CreateDataRepositoryTask";

test("4. gives every task its own identity instead of a module instance role", async () => {
  const all = await moduleStacks();
  const documents = new Map<string, string>();

  for (const { role, stack, name } of TASK_ROLES) {
    const resources = resourcesOf(all[stack] as Json);
    const [, task] = moduleTaskDefinition(resources, role);
    const taskProperties = properties(task, `${role} task`);
    const taskRoleId = resourceId(taskProperties["TaskRoleArn"]);
    assert.notEqual(taskRoleId, undefined, `${role} task role is a resource in this stack`);
    const taskRole = record(resources[taskRoleId ?? ""], `${role} task role`);
    assert.equal(taskRole["Type"], "AWS::IAM::Role");
    assert.equal(properties(taskRole, `${role} task role`)["RoleName"], name, `${role} task role name`);

    // The trust is the account-scoped task principal. The account is the replayed fixture's own,
    // so it is read off the template; the condition being there at all is the guard.
    const statements = record(
      properties(taskRole, `${role} task role`)["AssumeRolePolicyDocument"],
      "trust",
    )["Statement"];
    assert.ok(Array.isArray(statements) && statements.length === 1, `${role} has one trust statement`);
    const statement = record(statements[0], "trust statement");
    assert.deepEqual(statement["Principal"], { Service: "ecs-tasks.amazonaws.com" });
    assert.match(
      String(record(record(statement["Condition"], "condition")["StringEquals"], "equals")["aws:SourceAccount"]),
      /^\d{12}$/,
      `${role} task role trusts the task service only from this account`,
    );

    // A task role carries the managed policies the module instance role carries, so moving to its
    // own identity does not silently drop a permission the host role granted.
    const managed = (properties(taskRole, `${role} task role`)["ManagedPolicyArns"] as unknown[]).map((arn) =>
      JSON.stringify(arn),
    );
    assert.equal(managed.length, 2, `${role} task role carries two managed policies`);
    assert.ok(managed[0]?.includes("amazon-ssm-managed-instance-core"), `${role} session-manager policy`);
    assert.ok(managed[1]?.includes("cloud-watch-agent-server-policy"), `${role} metrics and logs policy`);

    // Each task policy came from its own module's template, and the four differ. The CDK-generated
    // default policy carries the command-execution grants rather than the module's own template,
    // so the rendered one is the one this asserts on.
    const [, policy] = findResource(resources, "AWS::IAM::Policy", (resource, id) => {
      if (id.includes("DefaultPolicy")) return false;
      const roles = properties(resource, "IAM policy")["Roles"];
      return Array.isArray(roles) && roles.some((entry) => refId(entry) === taskRoleId);
    });
    const document = JSON.stringify(properties(policy, `${role} policy`)["PolicyDocument"]);
    assert.ok(
      document.includes(`"${POLICY_MARKERS[role]}"`),
      `${role} task policy renders the ${role} template, the only one naming ${POLICY_MARKERS[role]}`,
    );
    documents.set(role, document);

    // The execution role is separate from the task role, so the running process never holds a
    // grant that only the task start needs.
    const executionRoleId = resourceId(taskProperties["ExecutionRoleArn"]);
    assert.notEqual(executionRoleId, undefined, `${role} names an execution role`);
    assert.notEqual(executionRoleId, taskRoleId, `${role} execution role is not its task role`);
  }
  assert.equal(new Set(documents.values()).size, 4, "four distinct task policies");

  // Rendering from the module template rather than from a second hand-written policy is what makes
  // a fix to that template reach the task. The container-only grants in the scheduler template are
  // the visible case: the user synchronisation that runs before the module starts, and the record
  // change the stable server name needs.
  const schedulerStatements = JSON.parse(documents.get("scheduler") as string) as Json;
  const statements = schedulerStatements["Statement"] as Json[];
  const userSync = statements.find((statement) => statement["Sid"] === "ClusterUserSync");
  assert.ok(userSync !== undefined, "the task policy carries the cluster user synchronisation grant");
  assert.deepEqual(userSync["Action"], "dynamodb:Scan", "the grant is a scan and nothing else");
  assert.equal(
    (userSync["Resource"] as string[]).length,
    3,
    "the grant names the three account tables the synchronisation reads",
  );
  assert.ok(
    statements.some((statement) => statement["Sid"] === "SchedulerDnsRecord"),
    "the task policy carries the record change the stable server name needs",
  );

  // The scheduler task policy names the roles it may pass, and both belong to this stack, so they
  // are references rather than names rebuilt by rule from outside.
  const schedulerResources = resourcesOf(all.scheduler as Json);
  const [, schedulerPolicy] = findResource(schedulerResources, "AWS::IAM::Policy", (resource, id) => {
    if (id.includes("DefaultPolicy")) return false;
    const roles = properties(resource, "IAM policy")["Roles"];
    return (
      Array.isArray(roles) &&
      roles.some((entry) => {
        const referenced = refId(entry);
        const role = referenced === undefined ? undefined : schedulerResources[referenced];
        return (
          role !== undefined &&
          properties(role, "role")["RoleName"] === `${FIXTURE_CLUSTER}-scheduler-task-role-${FIXTURE_REGION}`
        );
      })
    );
  });
  const passRoleNames = new Set<string>();
  const schedulerDocument = record(
    properties(schedulerPolicy, "scheduler task policy")["PolicyDocument"],
    "scheduler policy document",
  );
  for (const statement of (schedulerDocument["Statement"] ?? []) as Json[]) {
    if (!JSON.stringify(statement["Action"]).includes("iam:PassRole")) continue;
    for (const target of Array.isArray(statement["Resource"]) ? statement["Resource"] : [statement["Resource"]]) {
      const referenced = resourceId(target);
      const role = referenced === undefined ? undefined : schedulerResources[referenced];
      if (role !== undefined) passRoleNames.add(properties(role, "role")["RoleName"] as string);
    }
  }
  assert.deepEqual(
    [...passRoleNames].sort(),
    [
      `${FIXTURE_CLUSTER}-scheduler-compute-node-role-${FIXTURE_REGION}`,
      `${FIXTURE_CLUSTER}-scheduler-spot-fleet-request-role-${FIXTURE_REGION}`,
    ],
    "the compute node and spot fleet roles are resources of this stack, not names rebuilt by rule",
  );

  const request = requestText();
  assert.ok(request.includes("Remove the task principal from the module instance roles"));
  assert.ok(request.includes("no longer add `ecs-tasks` to a module instance role's trust"));
  await assertModuleInstanceRolesOmitTaskTrust();
});

test("5. makes host-level observability explicit and secret-scoped", async () => {
  const disabled = await moduleStacks();
  for (const stack of ["clusterManager", "scheduler", "vdc"] as const) {
    for (const [, task] of byType(resourcesOf(disabled[stack] as Json), "AWS::ECS::TaskDefinition")) {
      const taskProperties = properties(task, "Application task");
      const volumes = taskProperties["Volumes"];
      const volumeText = Array.isArray(volumes) ? JSON.stringify(volumes) : "";
      assert.ok(
        !volumeText.includes("/var/run/datadog"),
        "Disabled observability has no application socket mount",
      );
      for (const container of containers(task)) {
        assert.ok(
          !JSON.stringify(container["MountPoints"]).includes("/var/run/datadog"),
          "Disabled observability has no application socket mount point",
        );
      }
    }
  }

  assert.throws(
    () =>
      synth({
        datadogEnabled: true,
        datadogImage: "public.ecr.aws/example/agent:latest",
      }),
    /digest-pinned image reference/,
  );

  const enabledResources = resourcesOf(synth({ datadogEnabled: true }));
  const [, datadogTask] = findResource(
    enabledResources,
    "AWS::ECS::TaskDefinition",
    (resource) => JSON.stringify(resource).includes("\"Name\":\"DD_API_KEY\""),
  );
  const datadogProperties = properties(datadogTask, "Observability task");
  assert.equal(datadogProperties["NetworkMode"], "host");
  assert.equal(datadogProperties["PidMode"], "host");
  assert.ok(JSON.stringify(datadogTask).includes(DATADOG_DIGEST));
  assert.ok(JSON.stringify(datadogTask).includes("/var/run/docker.sock"));

  // With the flag on, every application task gets the socket read-only, so a task can publish a
  // metric and cannot replace the host socket.
  const withSocket: Array<[string, Json]> = [
    ["cluster-manager", (await synthClusterManagerWithEcs({ "ecs.datadog.enabled": { BOOL: true } })) as Json],
    ["scheduler", (await synthSchedulerWithEcs({ "ecs.datadog.enabled": { BOOL: true } })) as Json],
    ["vdc", (await synthVdcWithEcs({ "ecs.datadog.enabled": { BOOL: true } })) as Json],
  ];
  for (const { role, stack } of TASK_ROLES) {
    const template = withSocket.find(([name]) => name === (stack === "clusterManager" ? "cluster-manager" : stack));
    assert.ok(template !== undefined, `${role} template`);
    const [, task] = moduleTaskDefinition(resourcesOf(template[1]), role);
    const socketMounts = containers(task)
      .flatMap((container) => {
        const mounts = container["MountPoints"];
        return Array.isArray(mounts) ? mounts.map((mount) => record(mount, "Mount point is an object")) : [];
      })
      .filter((mount) => mount["ContainerPath"] === "/var/run/datadog");
    assert.equal(socketMounts.length, 1, `${role} has one metrics socket mount`);
    assert.equal(socketMounts[0]?.["ReadOnly"], true, `${role} cannot replace the host socket`);
  }

  const [, launchTemplate] = findResource(enabledResources, "AWS::EC2::LaunchTemplate", () => true);
  assert.ok(
    JSON.stringify(launchTemplate).includes("install -d -o root -g root -m 0755 /var/run/datadog"),
    "Host socket directory is not world-writable",
  );

  const policies = byType(enabledResources, "AWS::IAM::Policy");
  const secretPolicyByRole = new Map<string, string>();
  for (const [, policy] of policies) {
    const policyProperties = properties(policy, "IAM policy");
    if (!JSON.stringify(policyProperties).includes("secretsmanager:GetSecretValue")) continue;
    const roles = policyProperties["Roles"];
    if (!Array.isArray(roles)) throw new Error("Secret policy has no Roles");
    for (const role of roles) {
      const id = refId(role);
      if (id !== undefined) secretPolicyByRole.set(id, JSON.stringify(policyProperties));
    }
  }

  const datadogExecutionRole = resourceId(datadogProperties["ExecutionRoleArn"]);
  assert.notEqual(datadogExecutionRole, undefined);
  const datadogPolicy = secretPolicyByRole.get(datadogExecutionRole ?? "");
  assert.ok(datadogPolicy?.includes("synthetic-observability-secret"));

  // The gateway certificate belongs to the stack that builds the gateway task, so no role here
  // reads it and the observability key is the only secret the daemon's execution role holds.
  assert.equal(
    (datadogPolicy ?? "").includes("synthetic-gateway"),
    false,
    "the daemon execution role holds no gateway secret",
  );
  const templateText = JSON.stringify(enabledResources);
  for (const gatewaySecret of ["synthetic-gateway-certificate", "synthetic-gateway-private-key"]) {
    assert.equal(templateText.includes(gatewaySecret), false, `${gatewaySecret} is not read here`);
  }
});

test("6. gives no application task a host control path", async () => {
  const all = await moduleStacks();
  for (const { role, stack } of TASK_ROLES) {
    const [, task] = moduleTaskDefinition(resourcesOf(all[stack] as Json), role);
    const taskProperties = properties(task, `${role} task`);
    assert.equal(taskProperties["NetworkMode"], "awsvpc");
    assert.equal(taskProperties["PidMode"], undefined);
    assert.ok(!JSON.stringify(task).includes("/var/run/docker.sock"));
    for (const container of containers(task)) {
      assert.notEqual(container["Privileged"], true);
      assert.equal(container["LinuxParameters"], undefined);
    }
  }
});

test("7. records that the module instance roles no longer trust the task service", async () => {
  const request = requestText();
  for (const owner of [
    "src/cdk/stacks/cluster-manager.ts",
    "src/cdk/stacks/scheduler.ts",
    "src/cdk/stacks/vdc.ts",
  ]) {
    assert.ok(request.includes(owner), `the request names ${owner}`);
  }
  assert.ok(
    request.includes("no longer add `ecs-tasks` to a module instance role's trust"),
    "the request records the removal as done, not as an open ask",
  );
  assert.ok(
    request.includes("`retain_legacy_host_role_trust` that the previous item asked for is not needed"),
    "the staged removal flag is withdrawn",
  );
  assert.ok(
    request.includes("rollback to a revision that predates the task roles"),
    "the one case the withdrawn staging covered is recorded",
  );
  assert.ok(
    request.includes("Do not add a cluster `aws:SourceArn` condition"),
    "the account condition stays the confused-deputy boundary",
  );
  await assertModuleInstanceRolesOmitTaskTrust();
});

test("8. gives the desktop gateway task its own identity and no host control path", async () => {
  const resources = resourcesOf(await desktop());
  const [taskRoleId, executionRoleId] = GATEWAY_TASK_ROLES as [string, string];

  // The task role holds what the host role holds, so moving to its own identity drops no
  // permission the host granted.
  const taskRole = record(resources[taskRoleId], "gateway task role");
  const managed = (properties(taskRole, "gateway task role")["ManagedPolicyArns"] as unknown[]).map(
    (arn) => JSON.stringify(arn),
  );
  assert.equal(managed.length, 2, "the gateway task role carries two managed policies");
  assert.ok(managed[0]?.includes("amazon-ssm-managed-instance-core"), "session-manager policy");
  assert.ok(managed[1]?.includes("cloud-watch-agent-server-policy"), "metrics and logs policy");

  // Its policy is rendered from the gateway module template, not hand-written a second time.
  const inlinePolicies = byType(resources, "AWS::IAM::Policy").filter(([id, resource]) => {
    if (id.includes("DefaultPolicy")) return false;
    const roles = properties(resource, "IAM policy")["Roles"];
    return Array.isArray(roles) && roles.some((entry) => refId(entry) === taskRoleId);
  });
  assert.equal(inlinePolicies.length, 1, "one rendered policy on the gateway task role");
  const document = JSON.stringify(
    properties(inlinePolicies[0]![1], "gateway task policy")["PolicyDocument"],
  );
  assert.ok(
    document.includes(`"${GATEWAY_POLICY_MARKER}"`),
    `the gateway task policy renders the gateway template, the only one naming ${GATEWAY_POLICY_MARKER}`,
  );
  for (const [role, marker] of Object.entries(POLICY_MARKERS)) {
    assert.equal(document.includes(`"${marker}"`), false, `the gateway task policy is not the ${role} template`);
  }

  // The execution role reads the certificate pair at task start and nothing else. It is separate
  // from the task role, so the running process never holds the secret grant.
  assert.notEqual(taskRoleId, executionRoleId);
  const secretResources = byType(resources, "AWS::IAM::Policy")
    .filter(([, resource]) => {
      const roles = properties(resource, "IAM policy")["Roles"];
      return Array.isArray(roles) && roles.some((entry) => refId(entry) === executionRoleId);
    })
    .flatMap(([, resource]) => {
      const statements = record(
        properties(resource, "execution policy")["PolicyDocument"],
        "policy document",
      )["Statement"];
      return Array.isArray(statements) ? statements.map((entry) => record(entry, "statement")) : [];
    })
    .filter((statement) => JSON.stringify(statement["Action"]).includes("secretsmanager:GetSecretValue"))
    .map((statement) => JSON.stringify(statement["Resource"]));
  assert.deepEqual(
    secretResources,
    [
      ...GATEWAY_CERTIFICATE_ROWS.map((row) => JSON.stringify(settingsLookup(CONFIG_FILE)(row))),
    ],
    "the gateway execution role reads the certificate pair and no other secret",
  );

  const task = record(resources[GATEWAY_TASK_DEFINITION], "gateway task definition");
  const taskProperties = properties(task, "gateway task");
  assert.equal(taskProperties["NetworkMode"], "awsvpc");
  assert.equal(taskProperties["PidMode"], undefined);
  assert.ok(!JSON.stringify(task).includes("/var/run/docker.sock"));
  // The daemon socket is a host path, and the gateway runs no host-level observability.
  assert.ok(!JSON.stringify(task).includes("/var/run/datadog"));
  for (const container of containers(task)) {
    assert.notEqual(container["Privileged"], true);
    assert.equal(container["LinuxParameters"], undefined);
  }
});
