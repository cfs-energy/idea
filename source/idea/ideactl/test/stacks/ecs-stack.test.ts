/**
 * Container stack synthesis checks.
 *
 * The test uses only synthetic settings and a replay-free VPC context. It asserts the deployment
 * controls whose removal would make a container cutover unsafe.
 *
 * The five application services are not here: each is created by the module stack that publishes
 * the settings its application reads. Their controls are asserted against those stacks, in
 * `ecs-constraints.test.ts`, `security-hardening.test.ts` and the three module stack tests.
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
import { ECS_HOST_SETTINGS, ECS_TASK_SETTINGS } from "../support/ecs-settings.ts";

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

function settings(datadogEnabled = false, overrides: Json = {}): ClusterConfig {
  const values: Record<string, unknown> = {
    "cluster.aws.account_id": ACCOUNT,
    "cluster.aws.dns_suffix": "amazonaws.com",
    "cluster.aws.partition": "aws",
    "cluster.aws.region": REGION,
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
    "ecs.datadog.image": `${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/observability-agent@sha256:${"a".repeat(64)}`,
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
  return new ClusterConfig(Object.entries({ ...values, ...overrides }).map(([key, value]) => ({ key, value })));
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

function synth(datadogEnabled = false, overrides: Json = {}): Json {
  const outdir = mkdtempSync(join(tmpdir(), "ideactl-ecs-"));
  workdirs.push(outdir);
  const app = new App({ context: { "aws:cdk:enable-path-metadata": true, ...vpcContext() }, outdir });
  const config = settings(datadogEnabled, overrides);
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

test("emits the shared capacity and nothing an application stack owns", () => {
  const resources = resourcesOf(synth());

  assert.equal(byType(resources, "AWS::ECS::Cluster").length, 1, "cluster");
  assert.equal(byType(resources, "AWS::ServiceDiscovery::PrivateDnsNamespace").length, 1, "service-discovery namespace");
  assert.equal(byType(resources, "AWS::EC2::LaunchTemplate").length, 1, "host launch template");
  assert.equal(byType(resources, "AWS::AutoScaling::AutoScalingGroup").length, 1, "host auto scaling group");
  assert.equal(byType(resources, "AWS::ECS::CapacityProvider").length, 1, "capacity provider");
  assert.equal(byType(resources, "Custom::ClusterSettings").length, 1, "published settings");
  assert.equal(byType(resources, "Custom::ReleaseScaleInProtection").length, 1, "host scale-in release");

  // Every application task is created by the stack that publishes the rows its process reads at
  // boot, so none of these belongs here. On a fresh install those rows do not exist yet, which is
  // what a task created here would fail on.
  assert.equal(byType(resources, "AWS::ECS::TaskDefinition").length, 0, "no application task definitions");
  assert.equal(byType(resources, "AWS::ECS::Service").length, 0, "no application services");
  assert.equal(byType(resources, "AWS::ElasticLoadBalancingV2::TargetGroup").length, 0, "no target groups");
  assert.equal(byType(resources, "AWS::EFS::FileSystem").length, 0, "no scheduler state file system");
  assert.equal(byType(resources, "Custom::EcsEndpoint").length, 0, "no routed endpoints");
  assert.equal(byType(resources, "Custom::EcsDefaultEndpoint").length, 0, "no listener default actions");

  const OWNED_CUSTOM_TYPES = [
    "Custom::ClusterSettings",
    "Custom::ReleaseScaleInProtection",
    "Custom::EnsureLogGroup",
  ];
  assert.equal(
    Object.values(resources).filter(
      (resource) => text(resource["Type"]).startsWith("Custom::") && !OWNED_CUSTOM_TYPES.includes(text(resource["Type"])),
    ).length,
    0,
    "the container stack owns no other custom resources",
  );

  // One host role and nothing that trusts the task service: the task identities belong to the
  // module stacks, and with the observability daemon off this stack creates no task role at all.
  const trusting = byType(resources, "AWS::IAM::Role").filter(([, role]) =>
    JSON.stringify(record(role["Properties"], "role properties")["AssumeRolePolicyDocument"]).includes("ecs-tasks."),
  );
  assert.equal(trusting.length, 0, "no task identity without the observability daemon");
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

  const ensureResources = byType(resources, "Custom::EnsureLogGroup");
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

  // One packaged provider serves every group: the same handler the cluster-manager stack uses,
  // whose Delete leaves the group and its history in place.
  const providers = new Set(
    ensureResources.map(([, resource]) => JSON.stringify(record(resource["Properties"], "ensure properties")["ServiceToken"])),
  );
  assert.equal(providers.size, 1, "one ensure provider serves every group");
  const handler = findResource(resources, "AWS::Lambda::Function", (resource) =>
    text(record(resource["Properties"], "Lambda properties")["FunctionName"]).endsWith("-agent-log-group-lambda"),
  );
  const handlerProperties = record(handler["Properties"], "handler properties");
  assert.equal(handlerProperties["Runtime"], "nodejs22.x", "the ensure handler is the packaged Node port");
  assert.equal(handlerProperties["Handler"], "index.handler");
  assert.equal(
    record(handlerProperties["Code"], "handler code")["ZipFile"],
    undefined,
    "the handler is an asset, not an inline copy",
  );
});

test("keeps the observability task definition for a rollback and runs the daemon on the EC2 launch type", () => {
  const resources = resourcesOf(synth(true));
  for (const [id, task] of byType(resources, "AWS::ECS::TaskDefinition")) {
    assert.equal(task["DeletionPolicy"], "Retain", `${id} retained`);
    assert.equal(task["UpdateReplacePolicy"], "Retain", `${id} retained on replacement`);
  }
  // ECS refuses a capacity provider strategy on the DAEMON scheduling strategy (idea-dev27-ecs
  // rolled back on exactly that, 2026-09-15); a daemon names the launch type instead.
  for (const [id, service] of byType(resources, "AWS::ECS::Service")) {
    const properties = record(service["Properties"], `${id} properties`);
    assert.equal(properties["SchedulingStrategy"], "DAEMON", `${id} is a daemon`);
    assert.equal(properties["CapacityProviderStrategy"], undefined, `${id} names no capacity provider strategy`);
    assert.equal(properties["LaunchType"], "EC2", `${id} uses the EC2 launch type`);
  }
});

test("blocks task access to host metadata", () => {
  const resources = resourcesOf(synth());
  const launchTemplate = findResource(resources, "AWS::EC2::LaunchTemplate", () => true);
  const launchData = record(record(launchTemplate["Properties"], "launch template properties")["LaunchTemplateData"], "launch data");
  assert.equal(record(launchData["MetadataOptions"], "metadata options")["HttpTokens"], "required", "IMDSv2 required");
  assert.ok(
    JSON.stringify(launchData["UserData"]).includes("ECS_AWSVPC_BLOCK_IMDS=true"),
    "host user data blocks task access to instance metadata",
  );
  // Tasks inherit the host resolver; the desktop agents advertise short hostnames.
  assert.ok(
    JSON.stringify(launchData["UserData"]).includes("resolved.conf.d/idea-search-domain.conf"),
    "the host user data does not set the VPC search domain",
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
    0,
    "observability agent is omitted when disabled",
  );

  const enabled = resourcesOf(synth(true));
  const services = byType(enabled, "AWS::ECS::Service");
  assert.equal(services.length, 1, "the daemon is the only service this stack runs");
  assert.ok(
    services.some(([, service]) => record(service["Properties"], "service properties")["SchedulingStrategy"] === "DAEMON"),
    "observability service uses daemon scheduling",
  );

  // The tasks send to a socket on a host path; the agent listens on UDP unless told to open it
  // (idea-dev27 ran a day with every module's metrics going nowhere, 2026-09-15).
  const [, task] = byType(enabled, "AWS::ECS::TaskDefinition")[0]!;
  const container = record((record(task["Properties"], "task properties")["ContainerDefinitions"] as unknown[])[0], "agent container");
  const environment = Object.fromEntries(
    (container["Environment"] as Array<{ Name: string; Value: string }>).map((entry) => [entry.Name, entry.Value]),
  );
  assert.equal(environment["DD_DOGSTATSD_SOCKET"], "/var/run/datadog/dsd.socket");
  assert.equal(environment["DD_DOGSTATSD_ORIGIN_DETECTION"], "true");
  assert.ok(
    (container["MountPoints"] as Array<{ ContainerPath: string }>).some((mount) => mount.ContainerPath === "/var/run/datadog"),
    "the socket directory is mounted from the host",
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

test("records every command-execution session to a group this stack creates or adopts", () => {
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
  const ensuredGroupNames = byType(resources, "Custom::EnsureLogGroup").map(([, resource]) =>
    text(record(resource["Properties"], "ensure properties")["LogGroupName"]),
  );
  assert.ok(ensuredGroupNames.includes(execGroupName), "the session log group is created or adopted by this stack");
  const dependsOn = cluster["DependsOn"];
  const dependencies = Array.isArray(dependsOn) ? dependsOn : dependsOn === undefined ? [] : [dependsOn];
  assert.ok(
    dependencies.some((id) => typeof id === "string" && id.startsWith("execloggroup")),
    "the cluster waits for its session log group",
  );

  // The observability daemon is not an operator target, and it is the only service here.
  const datadog = byType(resources, "AWS::ECS::Service");
  assert.equal(datadog.length, 1, "the observability daemon runs as its own service");
  assert.equal(
    record(datadog[0]![1]["Properties"], "datadog properties")["EnableExecuteCommand"],
    undefined,
    "the observability daemon is not an operator shell target",
  );

  // The deployment tool never holds the caller-side permission.
  assert.ok(!JSON.stringify(resources).includes("ecs:ExecuteCommand"), "no template grants ecs:ExecuteCommand");
});

test("mounts the control plane's shared storage on the hosts as the host bootstrap did, before the cluster join", () => {
  const options = "nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport 0 0";
  const resources = resourcesOf(synth(false, {
    "storage.home.provider": "fsx_netapp_ontap",
    "storage.home.mount_dir": "/home",
    "storage.home.mount_options": options,
    "storage.home.scope": ["cluster"],
    "storage.home.fsx_netapp_ontap.svm.nfs_dns": "svm-0123456789abcdef0.fs-0123456789abcdef0.fsx.us-east-2.amazonaws.com",
    "storage.home.fsx_netapp_ontap.volume.volume_path": "/profiles/Users/User_Home_Folders",
    "storage.scratch.provider": "fsx_lustre",
    "storage.scratch.mount_dir": "/lustre",
    "storage.scratch.mount_options": "lustre defaults,noatime,flock,_netdev 0 0",
    "storage.scratch.scope": ["cluster"],
    "storage.scratch.fsx_lustre.dns": "fs-0123456789abcdef1.fsx.us-east-2.amazonaws.com",
    "storage.scratch.fsx_lustre.mount_name": "abcdefgh",
    "storage.nodes.provider": "fsx_netapp_ontap",
    "storage.nodes.mount_dir": "/nodes",
    "storage.nodes.scope": ["compute-node"],
    "storage.nodes.fsx_netapp_ontap.svm.nfs_dns": "svm-0123456789abcdef0.fs-0123456789abcdef0.fsx.us-east-2.amazonaws.com",
    "storage.nodes.fsx_netapp_ontap.volume.volume_path": "/nodes",
  }));
  const launchTemplate = findResource(resources, "AWS::EC2::LaunchTemplate", () => true);
  const launchData = record(record(launchTemplate["Properties"], "launch template properties")["LaunchTemplateData"], "launch data");
  const userData = JSON.stringify(launchData["UserData"]);
  // The export path and the fstab-shaped options, exactly as the host bootstrap wrote them
  // (a production cluster mounts two ONTAP volumes this way; the first container build mounted the bare SVM).
  assert.ok(userData.includes(`svm-0123456789abcdef0.fs-0123456789abcdef0.fsx.us-east-2.amazonaws.com:/profiles/Users/User_Home_Folders /home/ ${options}`), "ONTAP fstab entry");
  assert.ok(userData.includes("fs-0123456789abcdef1.fsx.us-east-2.amazonaws.com@tcp:/abcdefgh /lustre/ lustre defaults,noatime,flock,_netdev 0 0"), "Lustre fstab entry");
  assert.ok(userData.includes("dnf install -y lustre-client"), "the Lustre client is installed when a Lustre entry exists");
  assert.ok(userData.includes("mount -a"), "the entries are mounted");
  assert.ok(!userData.includes("/nodes"), "storage scoped to compute nodes is not mounted on the control plane hosts");
  const guard = userData.indexOf("mountpoint -q /home");
  const join = userData.indexOf("ECS_CLUSTER=");
  assert.ok(guard > 0 && join > guard, "a failed mount stops the host before it joins the cluster");
});

test("refuses to synthesize a host mount whose endpoint or path is missing", () => {
  assert.throws(
    () => synth(false, {
      "storage.home.provider": "fsx_netapp_ontap",
      "storage.home.mount_dir": "/home",
      "storage.home.fsx_netapp_ontap.svm.nfs_dns": "svm-0123456789abcdef0.fs-0123456789abcdef0.fsx.us-east-2.amazonaws.com",
    }),
    /shared-storage\.home: fsx_netapp_ontap needs its endpoint and path/,
  );
});
for (const [label, overrides, expected] of [
  ["customer-managed", { "cluster.kms.key_type": "customer-managed", "cluster.secretsmanager.kms_key_id": "test-key" }, true],
  ["default", {}, false],
  ["AWS-managed", { "cluster.kms.key_type": "aws-managed", "cluster.secretsmanager.kms_key_id": "test-key" }, false],
  ["missing key", { "cluster.kms.key_type": "customer-managed" }, false],
] as const) {
  test(`Datadog execution role grants scoped decryption for ${label} secrets`, () => {
    const resources = resourcesOf(synth(true, overrides));
    const task = record(byType(resources, "AWS::ECS::TaskDefinition")[0]![1]["Properties"], "task");
    const role = (record(task["ExecutionRoleArn"], "execution role")["Fn::GetAtt"] as string[])[0];
    const statements = byType(resources, "AWS::IAM::Policy").flatMap(([, resource]) => {
      const props = record(resource["Properties"], "policy");
      if (!(props["Roles"] as Json[]).some((ref) => ref["Ref"] === role)) return [];
      return record(props["PolicyDocument"], "document")["Statement"] as Json[];
    }).filter((statement) => JSON.stringify(statement["Action"]).includes("kms:Decrypt"));
    assert.equal(statements.length, expected ? 1 : 0);
    if (expected) {
      assert.deepEqual(statements[0], { Action: "kms:Decrypt", Effect: "Allow", Resource: `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-key` });
    }
  });
}
