/**
 * Production security checks for the shared ECS control-plane hosts.
 *
 * These tests synthesize only synthetic resources.
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
const APPLICATION_ROLES = ["cluster-manager", "scheduler", "vdc", "dcv-broker", "dcv-gateway"];
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
    // The container stack attaches its own target groups to the listeners that serve them, so it
    // reads the endpoint handler and the four listeners, all cluster-stack outputs.
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
    "ecs.hosts.instance_type": "m7g.large",
    "ecs.hosts.max": 4,
    "ecs.hosts.min": 3,
    "ecs.hosts.volume_size": 60,
    "ecs.image":
      "registry.example.invalid/control-plane@sha256:abcdef",
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

const MODULE_STACKS = [
  "cluster-manager.ts",
  "scheduler.ts",
  "vdc.ts",
] as const;

/** Requires the three module stacks not to grant the task service on an instance role. */
function assertModuleInstanceRolesOmitTaskTrust(): void {
  for (const name of MODULE_STACKS) {
    const source = readFileSync(
      new URL(`../../src/cdk/stacks/${name}`, import.meta.url),
      "utf8",
    );
    assert.equal(
      source.includes("ecs-tasks"),
      false,
      `${name} must not trust ecs-tasks on a module instance role`,
    );
  }
}

after(() => {
  for (const workdir of workdirs) {
    rmSync(workdir, { force: true, recursive: true });
  }
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

/** Every rule that applies to one security group, inline and standalone, as one sorted list. */
function securityGroupRules(
  resources: Record<string, Json>,
  groupName: string,
): { ingress: Json[]; egress: Json[] } {
  const [groupId, group] = findResource(
    resources,
    "AWS::EC2::SecurityGroup",
    (resource) => properties(resource, "Security group")["GroupName"] === groupName,
  );
  const names: Record<string, string> = {};
  for (const [id, resource] of byType(resources, "AWS::EC2::SecurityGroup")) {
    names[id] = properties(resource, "Security group")["GroupName"] as string;
  }
  /** A standalone rule keeps its own GroupId and names its peer by reference, so both are resolved. */
  const normalize = (rule: Json): Json => {
    const copy: Json = {};
    for (const [key, value] of Object.entries(rule)) {
      if (key === "GroupId") continue;
      if (key === "SourceSecurityGroupId" || key === "DestinationSecurityGroupId") {
        const referenced = resourceId(value);
        copy[key] = referenced === undefined ? value : (names[referenced] ?? referenced);
        continue;
      }
      copy[key] = value;
    }
    return copy;
  };
  const sorted = (rules: Json[]): Json[] =>
    [...rules].map(normalize).sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right)));
  const groupProperties = properties(group, "Security group");
  const inlineIngress = (groupProperties["SecurityGroupIngress"] ?? []) as Json[];
  const inlineEgress = (groupProperties["SecurityGroupEgress"] ?? []) as Json[];
  const standalone = (type: string): Json[] =>
    byType(resources, type)
      .map(([, resource]) => properties(resource, "Rule"))
      .filter((rule) => resourceId(rule["GroupId"]) === groupId);
  return {
    ingress: sorted([...inlineIngress, ...standalone("AWS::EC2::SecurityGroupIngress")]),
    egress: sorted([...inlineEgress, ...standalone("AWS::EC2::SecurityGroupEgress")]),
  };
}

const VPC_CIDR = "192.0.2.0/24";

/** The two egress rules every module host group carries. */
const TCP_EGRESS: Json[] = [
  { CidrIp: "0.0.0.0/0", Description: "Allow all egress for TCP", FromPort: 0, IpProtocol: "tcp", ToPort: 65535 },
  { CidrIpv6: "::/0", Description: "Allow all egress for TCP", FromPort: 0, IpProtocol: "tcp", ToPort: 65535 },
];

/** The pair the host groups of the directory-joined roles carry, ingress first. */
const DIRECTORY_INGRESS: Json = {
  CidrIp: VPC_CIDR,
  Description: "Allow UDP Traffic from VPC. Required for Directory Service",
  FromPort: 0,
  IpProtocol: "udp",
  ToPort: 1024,
};
const DIRECTORY_EGRESS: Json[] = [
  {
    CidrIp: "0.0.0.0/0",
    Description: "Allow UDP Traffic. Required for Directory Service",
    FromPort: 0,
    IpProtocol: "udp",
    ToPort: 1024,
  },
  {
    CidrIpv6: "::/0",
    Description: "Allow UDP Traffic. Required for Directory Service",
    FromPort: 0,
    IpProtocol: "udp",
    ToPort: 1024,
  },
];

const API_INGRESS: Json = {
  CidrIp: VPC_CIDR,
  Description: "Allow HTTP traffic from all VPC nodes for API access",
  FromPort: 8443,
  IpProtocol: "tcp",
  ToPort: 8443,
};

/** Desktop client ingress, one pair per prefix list the host gateway group allows. */
function prefixListIngress(prefixListId: string): Json[] {
  return [
    {
      Description: "Allow TCP traffic access from Prefix List to DCV Connection Gateway",
      FromPort: 8443,
      IpProtocol: "tcp",
      SourcePrefixListId: prefixListId,
      ToPort: 8443,
    },
    {
      Description: "Allow UDP traffic access from Prefix List to DCV Connection Gateway",
      FromPort: 8443,
      IpProtocol: "udp",
      SourcePrefixListId: prefixListId,
      ToPort: 8443,
    },
  ];
}

/**
 * The position each task group holds, rule by rule, against the module host group it replaces.
 *
 * Every rule here was read from the deployed templates of the development cluster and of the two
 * clusters carrying real user load. Two host rules are deliberately absent from every task group:
 * ssh from the bastion group, because a task runs no ssh daemon and the module stacks already remove
 * that rule under the container flag, and the separate 8443 allowance for the external load balancer
 * group, because it is a subset of the API rule and every load balancer is inside this VPC. Where a
 * host group opens all traffic from the VPC, which the controller and gateway groups do, the task
 * rule names the ports the task listens on instead.
 */
function expectedTaskGroupRules(quicSupported: boolean): Record<string, { ingress: Json[]; egress: Json[] }> {
  const self = `${CLUSTER}-ecs-dcv-broker-task-security-group`;
  const brokerDiscovery = [47100, 47200, 47500].map((port) => ({
    Description: `Allow broker to broker port ${port}`,
    FromPort: port,
    IpProtocol: "tcp",
    SourceSecurityGroupId: self,
    ToPort: port,
  }));
  return {
    // Web portal group: API rule plus the directory pair.
    "cluster-manager": {
      ingress: [API_INGRESS, DIRECTORY_INGRESS],
      egress: [...TCP_EGRESS, ...DIRECTORY_EGRESS],
    },
    // Controller group: API rule plus the directory pair. Its all-traffic rule from the VPC is
    // narrowed to the API port the controller listens on.
    vdc: {
      ingress: [API_INGRESS, DIRECTORY_INGRESS],
      egress: [...TCP_EGRESS, ...DIRECTORY_EGRESS],
    },
    // Scheduler group: the batch protocol uses reserved and ephemeral ports, so the host group's
    // all-TCP rule is carried as it stands.
    scheduler: {
      ingress: [
        API_INGRESS,
        {
          CidrIp: VPC_CIDR,
          Description: "Allow all TCP traffic from VPC to scheduler",
          FromPort: 0,
          IpProtocol: "tcp",
          ToPort: 65535,
        },
        DIRECTORY_INGRESS,
      ],
      egress: [...TCP_EGRESS, ...DIRECTORY_EGRESS],
    },
    // Broker group: no API rule, the three broker ports from the VPC, the three discovery ports from
    // itself, and no directory pair, exactly as the host group.
    "dcv-broker": {
      ingress: [
        {
          CidrIp: VPC_CIDR,
          Description: "Allow VPC to broker ports 8444-8446",
          FromPort: 8444,
          IpProtocol: "tcp",
          ToPort: 8446,
        },
        ...brokerDiscovery,
      ],
      egress: [...TCP_EGRESS],
    },
    // Gateway group: the API port, the QUIC port, the health check port, and desktop clients from
    // both prefix lists. The UDP egress pair exists only where the QUIC transport is on, which is
    // the one place the three clusters disagree.
    "dcv-gateway": {
      ingress: [
        API_INGRESS,
        {
          CidrIp: VPC_CIDR,
          Description: "Allow UDP traffic from all VPC nodes for the QUIC transport",
          FromPort: 8443,
          IpProtocol: "udp",
          ToPort: 8443,
        },
        {
          CidrIp: VPC_CIDR,
          Description: "Allow TCP traffic access for HealthCheck to DCV Connection Gateway",
          FromPort: 8989,
          IpProtocol: "tcp",
          ToPort: 8989,
        },
        ...prefixListIngress(CLUSTER_PREFIX_LIST),
        ...prefixListIngress(OPERATOR_PREFIX_LIST),
      ],
      egress: quicSupported
        ? [
            ...TCP_EGRESS,
            {
              CidrIp: "0.0.0.0/0",
              Description: "Allow all egress for UDP for QUIC Support on DCV Connection Gateway",
              FromPort: 0,
              IpProtocol: "udp",
              ToPort: 65535,
            },
            {
              CidrIpv6: "::/0",
              Description: "Allow all egress for UDP for QUIC Support on DCV Connection Gateway",
              FromPort: 0,
              IpProtocol: "udp",
              ToPort: 65535,
            },
          ]
        : [...TCP_EGRESS],
    },
  };
}

/** Sorts an expected rule list the way the collected rules are sorted. */
function sortRules(rules: Json[]): Json[] {
  return [...rules].sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right)));
}

test("2. positions each task ENI where the module host group it replaces sits", () => {
  for (const quicSupported of [false, true]) {
    const resources = resourcesOf(synth({ quicSupported }));
    const expected = expectedTaskGroupRules(quicSupported);
    for (const [role, rules] of Object.entries(expected)) {
      const actual = securityGroupRules(resources, `${CLUSTER}-ecs-${role}-task-security-group`);
      assert.deepEqual(actual.ingress, sortRules(rules.ingress), `${role} task group ingress, quic=${quicSupported}`);
      assert.deepEqual(actual.egress, sortRules(rules.egress), `${role} task group egress, quic=${quicSupported}`);
    }

    // Each service carries its own task group and nothing else, and the host group stays off the
    // task interfaces. A module group id from the settings must not appear anywhere.
    for (const role of Object.keys(expected)) {
      const [taskId] = taskDefinition(resources, role);
      const [, service] = findResource(
        resources,
        "AWS::ECS::Service",
        (resource) => refId(properties(resource, "ECS service")["TaskDefinition"]) === taskId,
      );
      const awsvpc = record(
        record(
          properties(service, `${role} service`)["NetworkConfiguration"],
          `${role} service has NetworkConfiguration`,
        )["AwsvpcConfiguration"],
        `${role} service has AwsvpcConfiguration`,
      );
      const attached = awsvpc["SecurityGroups"];
      assert.ok(Array.isArray(attached) && attached.length === 1, `${role} task ENI has one group`);
      const groupId = resourceId(attached[0]);
      assert.equal(
        properties(record(resources[groupId ?? ""], `${role} group`), "Group")["GroupName"],
        `${CLUSTER}-ecs-${role}-task-security-group`,
        `${role} task ENI carries its own task group`,
      );
    }

    const template = JSON.stringify(resources);
    for (const moduleGroupId of MODULE_SECURITY_GROUP_IDS) {
      assert.equal(
        template.includes(moduleGroupId),
        false,
        `${moduleGroupId} is a module group this stack no longer reads`,
      );
    }

    const [hostGroupId, hostGroup] = findResource(
      resources,
      "AWS::EC2::SecurityGroup",
      (resource) => properties(resource, "Security group")["GroupDescription"] === "Security group for ECS container hosts",
    );
    assert.equal(properties(hostGroup, "Host security group")["SecurityGroupIngress"], undefined);
    for (const [, ingress] of byType(resources, "AWS::EC2::SecurityGroupIngress")) {
      const rule = properties(ingress, "Ingress");
      if (resourceId(rule["GroupId"]) !== hostGroupId) continue;
      assert.fail("Host group must not receive application listener traffic");
    }
  }
});

test("3. keeps PBS state on a scheduler-only encrypted EFS access point", () => {
  const resources = resourcesOf(synth());
  const [fileSystemId, fileSystem] = findResource(
    resources,
    "AWS::EFS::FileSystem",
    () => true,
  );
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
      JSON.stringify(statement).includes(
        "\"ArnNotEquals\":{\"aws:PrincipalArn\"",
      ),
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
      JSON.stringify(statement).includes(
        "\"StringNotLike\":{\"elasticfilesystem:AccessPointArn\"",
      ),
    ),
    "Policy denies mounts outside the scheduler access point",
  );
  const schedulerTaskRole = findResource(
    resources,
    "AWS::IAM::Role",
    (resource) =>
      properties(resource, "Role")["RoleName"] === `${CLUSTER}-ecs-scheduler-task-role-${REGION}`,
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
        JSON.stringify(record_["Condition"]).includes(
          "\"elasticfilesystem:AccessedViaMountTarget\"",
        )
      );
    }),
    "Policy allows the scheduler task role, and only through a mount target",
  );

  const [, accessPoint] = findResource(
    resources,
    "AWS::EFS::AccessPoint",
    () => true,
  );
  assert.equal(accessPoint["DeletionPolicy"], "Retain");
  assert.equal(accessPoint["UpdateReplacePolicy"], "Retain");
  const accessPointProperties = properties(accessPoint, "PBS access point");
  assert.deepEqual(accessPointProperties["PosixUser"], {
    Gid: "0",
    Uid: "0",
  });
  assert.ok(
    JSON.stringify(accessPointProperties["RootDirectory"]).includes(
      "\"Permissions\":\"0700\"",
    ),
  );

  const [schedulerId, scheduler] = taskDefinition(resources, "scheduler");
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
    record(
      efsConfiguration["AuthorizationConfig"],
      "PBS volume has AuthorizationConfig",
    )["IAM"],
    "ENABLED",
  );
  assert.ok(
    JSON.stringify(containers(scheduler)).includes(
      "\"Name\":\"PBS_HOME\",\"Value\":\"/var/spool/pbs\"",
    ),
  );
  assert.ok(
    JSON.stringify(containers(scheduler)).includes(
      "\"ContainerPath\":\"/var/spool/pbs\"",
    ),
  );
  // The mount can be made from the task interface or from the container host interface, depending on
  // the container agent, and it fails silently from the wrong one. Both peers are groups this stack
  // owns, and the file system policy above is what limits access to the scheduler task role.
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
      Source: groupNameOf(rule["SourceSecurityGroupId"]),
    }))
    .sort((left, right) => String(left.Source).localeCompare(String(right.Source)));
  assert.deepEqual(nfsPeers, [
    {
      IpProtocol: "tcp",
      FromPort: 2049,
      ToPort: 2049,
      Source: `${CLUSTER}-ecs-host-security-group`,
    },
    {
      IpProtocol: "tcp",
      FromPort: 2049,
      ToPort: 2049,
      Source: `${CLUSTER}-ecs-scheduler-task-security-group`,
    },
  ]);

  for (const [id, task] of byType(
    resources,
    "AWS::ECS::TaskDefinition",
  )) {
    if (id === schedulerId) continue;
    assert.ok(
      !JSON.stringify(task).includes("scheduler-pbs"),
      `${id} cannot mount PBS state`,
    );
  }
});

/** The task role name this stack gives one container role. */
function taskRoleName(role: string): string {
  return `${CLUSTER}-ecs-${role}-task-role-${REGION}`;
}

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
  "dcv-gateway": "fsx:CreateDataRepositoryTask",
};

test("4. gives every task its own identity instead of a module instance role", () => {
  const resources = resourcesOf(synth({ datadogEnabled: true }));
  const executionRoleByApplication = new Map<string, string>();
  const taskRoleIds = new Map<string, string>();

  for (const role of APPLICATION_ROLES) {
    const [, task] = taskDefinition(resources, role);
    const taskProperties = properties(task, `${role} task`);
    const taskRoleId = resourceId(taskProperties["TaskRoleArn"]);
    assert.notEqual(taskRoleId, undefined, `${role} task role is a resource in this stack`);
    const taskRole = record(resources[taskRoleId ?? ""], `${role} task role`);
    assert.equal(taskRole["Type"], "AWS::IAM::Role");
    assert.equal(
      properties(taskRole, `${role} task role`)["RoleName"],
      taskRoleName(role),
      `${role} runs as the task role this stack creates`,
    );
    taskRoleIds.set(role, taskRoleId as string);

    // The trust is the account-scoped task principal, statement for statement.
    assert.deepEqual(
      record(properties(taskRole, `${role} task role`)["AssumeRolePolicyDocument"], "trust")["Statement"],
      [
        {
          Action: "sts:AssumeRole",
          Condition: { StringEquals: { "aws:SourceAccount": ACCOUNT } },
          Effect: "Allow",
          Principal: { Service: "ecs-tasks.amazonaws.com" },
        },
      ],
      `${role} task role trusts the task service only from this account`,
    );

    const executionRoleId = resourceId(taskProperties["ExecutionRoleArn"]);
    assert.notEqual(executionRoleId, undefined);
    if (executionRoleId !== undefined) executionRoleByApplication.set(role, executionRoleId);
  }
  assert.equal(new Set(taskRoleIds.values()).size, 5, "five distinct task roles");

  // The module instance roles are not the task identity any more, in either spelling: the ARN a
  // setting carries, or the physical name the module stack gives the role.
  const template = JSON.stringify(resources);
  for (const moduleRole of [
    "synthetic-cluster-manager-role",
    "synthetic-scheduler-role",
    "synthetic-vdc-role",
    "synthetic-broker-role",
    "synthetic-gateway-role",
  ]) {
    assert.equal(template.includes(moduleRole), false, `${moduleRole} is not referenced by this stack`);
  }
  for (const moduleRoleName of [
    `${CLUSTER}-cm-role-${REGION}`,
    `${CLUSTER}-scheduler-role-${REGION}`,
    `${CLUSTER}-vdc-controller-role-${REGION}`,
    `${CLUSTER}-vdc-broker-role-${REGION}`,
    `${CLUSTER}-vdc-gateway-role-${REGION}`,
  ]) {
    assert.equal(template.includes(moduleRoleName), false, `${moduleRoleName} is not referenced by this stack`);
  }

  // Each task policy came from its own module's template, and the five differ.
  const documents = new Map<string, string>();
  for (const role of APPLICATION_ROLES) {
    // Skip the CDK-generated default policy, which carries the command-execution grants rather
    // than the module's own template. The rendered template is the one this asserts on.
    const [, policy] = findResource(resources, "AWS::IAM::Policy", (resource, id) => {
      if (id.includes("DefaultPolicy")) return false;
      const roles = properties(resource, "IAM policy")["Roles"];
      return Array.isArray(roles) && roles.some((entry) => refId(entry) === taskRoleIds.get(role));
    });
    const document = JSON.stringify(properties(policy, `${role} policy`)["PolicyDocument"]);
    assert.ok(
      document.includes(`"${POLICY_MARKERS[role]}"`),
      `${role} task policy renders the ${role} template, which is the only one naming ${POLICY_MARKERS[role]}`,
    );
    documents.set(role, document);
  }
  for (const role of ["cluster-manager", "vdc", "scheduler", "dcv-broker"]) {
    assert.equal(
      documents.get("dcv-gateway")?.includes(`"${POLICY_MARKERS[role]}"`),
      false,
      `the gateway task policy is not the ${role} template`,
    );
  }
  assert.equal(new Set(documents.values()).size, 5, "five distinct task policies");

  // A task role carries the managed policies the module instance role carries, so moving to its own
  // identity does not silently drop a permission the host role granted.
  for (const role of APPLICATION_ROLES) {
    const taskRole = record(resources[taskRoleIds.get(role) ?? ""], `${role} task role`);
    const managed = (properties(taskRole, `${role} task role`)["ManagedPolicyArns"] as unknown[]).map(
      (arn) => JSON.stringify(arn),
    );
    assert.equal(managed.length, 2, `${role} task role carries two managed policies`);
    assert.ok(
      managed[0]?.includes("policy/synthetic-ssm-policy"),
      `${role} task role carries the instance session-manager policy`,
    );
    assert.ok(
      managed[1]?.includes("policy/synthetic-cloudwatch-policy"),
      `${role} task role carries the instance metrics and logs policy`,
    );
  }

  const baseExecutionRole = executionRoleByApplication.get("cluster-manager");
  const gatewayExecutionRole = executionRoleByApplication.get("dcv-gateway");
  assert.notEqual(baseExecutionRole, undefined);
  assert.notEqual(gatewayExecutionRole, undefined);
  assert.notEqual(baseExecutionRole, gatewayExecutionRole);
  for (const role of ["scheduler", "vdc", "dcv-broker"]) {
    assert.equal(executionRoleByApplication.get(role), baseExecutionRole);
  }

  for (const [id, role] of byType(resources, "AWS::IAM::Role")) {
    if (!id.includes("taskexecutionrole")) continue;
    const trust = record(
      properties(role, `${id} role`)["AssumeRolePolicyDocument"],
      `${id} has trust policy`,
    );
    assert.ok(
      JSON.stringify(trust).includes(
        `"StringEquals":{"aws:SourceAccount":"${ACCOUNT}"}`,
      ),
      `${id} trust is account-scoped`,
    );
  }

  // Rendering from the module template rather than from a second hand-written policy is what makes a
  // fix to that template reach the task. The container-only grants in the scheduler template are the
  // visible case: the user synchronisation that runs before the module starts, and the record change
  // the stable server name needs.
  const [, schedulerPolicy] = findResource(resources, "AWS::IAM::Policy", (resource, id) => {
    if (id.includes("DefaultPolicy")) return false;
    const roles = properties(resource, "IAM policy")["Roles"];
    return Array.isArray(roles) && roles.some((entry) => refId(entry) === taskRoleIds.get("scheduler"));
  });
  const schedulerStatements = record(
    properties(schedulerPolicy, "scheduler task policy")["PolicyDocument"],
    "policy document",
  )["Statement"] as Json[];
  const userSync = schedulerStatements.find((statement) => statement["Sid"] === "ClusterUserSync");
  assert.ok(userSync !== undefined, "the task policy carries the cluster user synchronisation grant");
  assert.deepEqual(userSync["Action"], "dynamodb:Scan", "the grant is a scan and nothing else");
  assert.equal(
    (userSync["Resource"] as string[]).length,
    3,
    "the grant names the three account tables the synchronisation reads",
  );
  assert.ok(
    schedulerStatements.some((statement) => statement["Sid"] === "SchedulerDnsRecord"),
    "the task policy carries the record change the stable server name needs",
  );
  const request = requestText();
  assert.ok(request.includes("Remove the task principal from the module instance roles"));
  assert.ok(request.includes("no longer add `ecs-tasks` to a module instance role's trust"));
  assertModuleInstanceRolesOmitTaskTrust();
});

test("5. makes host-level observability explicit and secret-scoped", () => {
  const disabledResources = resourcesOf(synth());
  for (const [, task] of byType(
    disabledResources,
    "AWS::ECS::TaskDefinition",
  )) {
    const taskProperties = properties(task, "Application task");
    const volumes = taskProperties["Volumes"];
    const volumeText = Array.isArray(volumes) ? JSON.stringify(volumes) : "";
    assert.ok(
      !volumeText.includes("/var/run/datadog"),
      "Disabled observability has no application socket mount",
    );
    for (const container of containers(task)) {
      assert.ok(
        !JSON.stringify(container["MountPoints"]).includes(
          "/var/run/datadog",
        ),
        "Disabled observability has no application socket mount point",
      );
    }
  }

  assert.throws(
    () =>
      synth({
        datadogEnabled: true,
        datadogImage: "public.ecr.aws/example/agent:latest",
      }),
    /digest-pinned private ECR image/,
  );

  const enabledResources = resourcesOf(synth({ datadogEnabled: true }));
  const [, datadogTask] = findResource(
    enabledResources,
    "AWS::ECS::TaskDefinition",
    (resource) =>
      JSON.stringify(resource).includes("\"Name\":\"DD_API_KEY\""),
  );
  const datadogProperties = properties(datadogTask, "Observability task");
  assert.equal(datadogProperties["NetworkMode"], "host");
  assert.equal(datadogProperties["PidMode"], "host");
  assert.ok(JSON.stringify(datadogTask).includes(DATADOG_DIGEST));
  assert.ok(JSON.stringify(datadogTask).includes("/var/run/docker.sock"));

  for (const role of [
    "cluster-manager",
    "scheduler",
    "vdc",
    "dcv-broker",
    "dcv-gateway",
  ]) {
    const [, task] = taskDefinition(enabledResources, role);
    const socketMounts = containers(task).flatMap((container) => {
      const mounts = container["MountPoints"];
      return Array.isArray(mounts)
        ? mounts.map((mount) => record(mount, "Mount point is an object"))
        : [];
    }).filter((mount) => mount["ContainerPath"] === "/var/run/datadog");
    assert.equal(socketMounts.length, 1, `${role} has one metrics socket mount`);
    assert.equal(
      socketMounts[0]?.["ReadOnly"],
      true,
      `${role} cannot replace the host socket`,
    );
  }

  const [, launchTemplate] = findResource(
    enabledResources,
    "AWS::EC2::LaunchTemplate",
    () => true,
  );
  assert.ok(
    JSON.stringify(launchTemplate).includes(
      "install -d -o root -g root -m 0755 /var/run/datadog",
    ),
    "Host socket directory is not world-writable",
  );

  const policies = byType(enabledResources, "AWS::IAM::Policy");
  const secretPolicyByRole = new Map<string, string>();
  for (const [, policy] of policies) {
    const policyProperties = properties(policy, "IAM policy");
    if (!JSON.stringify(policyProperties).includes("secretsmanager:GetSecretValue")) {
      continue;
    }
    const roles = policyProperties["Roles"];
    if (!Array.isArray(roles)) throw new Error("Secret policy has no Roles");
    for (const role of roles) {
      const id = refId(role);
      if (id !== undefined) {
        secretPolicyByRole.set(id, JSON.stringify(policyProperties));
      }
    }
  }

  const [, gatewayTask] = taskDefinition(enabledResources, "dcv-gateway");
  const gatewayExecutionRole = resourceId(
    properties(gatewayTask, "Gateway task")["ExecutionRoleArn"],
  );
  const datadogExecutionRole = resourceId(
    datadogProperties["ExecutionRoleArn"],
  );
  assert.notEqual(gatewayExecutionRole, undefined);
  assert.notEqual(datadogExecutionRole, undefined);
  assert.notEqual(gatewayExecutionRole, datadogExecutionRole);

  const gatewayPolicy = secretPolicyByRole.get(gatewayExecutionRole ?? "");
  const datadogPolicy = secretPolicyByRole.get(datadogExecutionRole ?? "");
  assert.ok(gatewayPolicy?.includes("synthetic-gateway-certificate"));
  assert.ok(gatewayPolicy?.includes("synthetic-gateway-private-key"));
  assert.ok(!gatewayPolicy?.includes("synthetic-observability-secret"));
  assert.ok(datadogPolicy?.includes("synthetic-observability-secret"));
  assert.ok(!datadogPolicy?.includes("synthetic-gateway"));

  const [, clusterManagerTask] = taskDefinition(
    enabledResources,
    "cluster-manager",
  );
  const baseExecutionRole = resourceId(
    properties(clusterManagerTask, "Cluster-manager task")[
      "ExecutionRoleArn"
    ],
  );
  assert.equal(secretPolicyByRole.get(baseExecutionRole ?? ""), undefined);
});

test("6. gives no application task a host control path", () => {
  const resources = resourcesOf(synth({ datadogEnabled: true }));
  for (const role of [
    "cluster-manager",
    "scheduler",
    "vdc",
    "dcv-broker",
    "dcv-gateway",
  ]) {
    const [, task] = taskDefinition(resources, role);
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

test("7. records that the module instance roles no longer trust the task service", () => {
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
  assertModuleInstanceRolesOmitTaskTrust();
});
