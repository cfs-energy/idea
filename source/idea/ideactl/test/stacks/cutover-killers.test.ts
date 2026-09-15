/**
 * Cutover blockers in the container control plane.
 *
 * Two of them are proved by synthesis: every planned target group name has to fit the longest
 * cluster name the tool accepts, and the broker has to receive its single-address-family option
 * under a name something actually reads. The third runs the shipped role scripts, so the claim is
 * about an observed process environment and a rendered configuration file rather than a literal in
 * the stack.
 *
 * The target groups and the broker task belong to the module stacks, which are synthesized from
 * the captured replay fixtures. The host image and the architecture the tasks follow belong to the
 * container stack, which is synthesized from synthetic settings under the worst-case cluster name.
 */

import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { isIPv4 } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import { fileURLToPath } from "node:url";
import { App, Aws, Fn } from "aws-cdk-lib";

import { makeContext } from "../../src/cdk/constructs/base.ts";
import { EcsStack } from "../../src/cdk/stacks/ecs.ts";
import type { SynthReads } from "../../src/cdk/synth-reads.ts";
import { ClusterConfig } from "../../src/config/cluster-config.ts";
import { getTargetGroupName } from "../../src/util/names.ts";
import { PUBLIC_CHECKOUT_ENV, requireCapture, requiredService } from "../support/fixtures.ts";
import { ideaVersion } from "../../src/version.ts";
import { ECS_HOST_SETTINGS, ECS_TASK_SETTINGS } from "../support/ecs-settings.ts";
import {
  CONFIG_FILE,
  CONTEXT_FILE,
  SYNTH_READS_FILE,
  cleanupWorkdirs,
  synthVdcWithEcs,
} from "../support/ecs-harness.ts";

requireCapture(
  [CONFIG_FILE, SYNTH_READS_FILE, CONTEXT_FILE],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

type JsonObject = Record<string, unknown>;

const ACCOUNT = "123456789012";
const REGION = "us-east-2";
/** `cluster_name cannot be more than 11 characters`, so this is the worst case a cluster can be. */
const LONGEST_CLUSTER = "idea-test11";
const TARGET_GROUP_NAME_LIMIT = 32;
const PREFER_IPV4 = "-Djava.net.preferIPv4Stack=true";
const IMAGE_ROLES = fileURLToPath(
  new URL("../../../../../deployment/ecr/idea-control-plane/roles/", import.meta.url),
);
const workdirs: string[] = [];

const JAVA_SETUP =
  "brew install openjdk, or install any JDK and put its bin directory ahead of /usr/bin on PATH";

/**
 * Returns the first java on this machine that launches. The macOS wrapper at /usr/bin/java
 * resolves first on PATH and exits with an error when no runtime is installed, so the name
 * alone does not settle whether there is a runtime.
 */
function workingJava(): string | undefined {
  for (const candidate of [
    "java",
    "/opt/homebrew/opt/openjdk/bin/java",
    "/usr/local/opt/openjdk/bin/java",
  ]) {
    try {
      execFileSync(candidate, ["-version"], { stdio: "ignore" });
      return candidate;
    } catch {
      continue;
    }
  }
  return undefined;
}

const JAVA = workingJava();
// A missing runtime is a failure naming its setup command, not a quiet pass. Only a checkout
// that declares it has no prerequisites skips.
const javaSkip: string | false =
  JAVA === undefined && process.env[PUBLIC_CHECKOUT_ENV] === "1"
    ? "no Java runtime, public checkout"
    : false;

const SYNTH_READS: SynthReads = {
  callerIdentity: async () => ({ account: ACCOUNT, arn: "synthetic-caller" }),
  describeDomain: async () => {
    throw new Error("synthetic ECS synthesis does not read domain settings");
  },
  describeListener: async () => ({}),
  describeUserPool: async () => ({}),
  listServiceLinkedRoles: async () => [],
};

/** Builds a partition-aware synthetic ARN without committing a concrete ARN. */
function syntheticArn(service: string, resource: string): string {
  const region = service === "iam" ? "" : REGION;
  return Fn.join("", ["arn:", Aws.PARTITION, ":", service, ":", region, ":", Aws.ACCOUNT_ID, ":", resource]);
}

/** Supplies deterministic VPC lookup data for offline synthesis. */
function vpcContext(): JsonObject {
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

/** Supplies only the settings required to synthesize the ECS stack. */
function settings(cluster: string, overrides: Record<string, unknown> = {}): ClusterConfig {
  const values: Record<string, unknown> = {
    "cluster.aws.account_id": ACCOUNT,
    "cluster.aws.dns_suffix": "amazonaws.com",
    "cluster.aws.partition": "aws",
    "cluster.aws.region": REGION,
    "cluster.cluster_name": cluster,
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
    "ecs.datadog.enabled": false,
    "ecs.datadog.image": "registry.example.invalid/agent:1",
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
  return new ClusterConfig(
    Object.entries({ ...values, ...overrides }).map(([key, value]) => ({ key, value })),
  );
}

/** Reports whether an unknown value is a JSON object. */
function isRecord(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Narrows an unknown synthesized value to a JSON object. */
function record(value: unknown, description: string): JsonObject {
  if (!isRecord(value)) throw new Error(description);
  return value;
}

/** Synthesizes the ECS stack for one cluster name without reading live services. */
function synthesize(cluster: string, overrides: Record<string, unknown> = {}): JsonObject {
  const outdir = mkdtempSync(join(tmpdir(), "ideactl-cutover-killers-"));
  workdirs.push(outdir);
  const app = new App({ context: { "aws:cdk:enable-path-metadata": true, ...vpcContext() }, outdir });
  new EcsStack({
    app,
    ctx: makeContext({
      awsRegion: REGION,
      config: settings(cluster, overrides),
      moduleId: "ecs",
      releaseVersion: ideaVersion(),
      synthReads: SYNTH_READS,
    }),
    deploymentId: "synthetic-deployment",
    env: { account: ACCOUNT, region: REGION },
    moduleName: "ecs",
    terminationProtection: true,
  });
  return record(app.synth().getStackByName(`${cluster}-ecs`).template, "synthesized template must be an object");
}

/** Every resource of one type in a synthesized template. */
function byType(template: JsonObject, type: string): JsonObject[] {
  const resources = record(template["Resources"], "template must contain resources");
  return Object.values(resources)
    .map((resource) => record(resource, "resource must be an object"))
    .filter((resource) => resource["Type"] === type);
}

/** The application container of one IDEA role, with its environment as a plain object. */
function roleEnvironment(template: JsonObject, role: string): Record<string, string> {
  for (const taskDefinition of byType(template, "AWS::ECS::TaskDefinition")) {
    const properties = record(taskDefinition["Properties"], "task definition must contain properties");
    const containers = properties["ContainerDefinitions"];
    if (!Array.isArray(containers)) throw new Error("task definition must contain container definitions");
    for (const containerValue of containers) {
      const container = record(containerValue, "container definition must be an object");
      const entries = container["Environment"];
      if (!Array.isArray(entries)) continue;
      const environment: Record<string, string> = {};
      for (const entryValue of entries) {
        const entry = record(entryValue, "environment entry must be an object");
        const name = entry["Name"];
        const value = entry["Value"];
        if (typeof name === "string" && typeof value === "string") environment[name] = value;
      }
      if (environment["IDEA_CONTAINER_ROLE"] === role) return environment;
    }
  }
  throw new Error(`task definition for ${role} was not synthesized`);
}

/** Writes an executable stub onto a directory that is prepended to PATH. */
function stub(directory: string, name: string, body: string): void {
  const path = join(directory, name);
  writeFileSync(path, `#!/bin/bash\n${body}\n`);
  chmodSync(path, 0o755);
}

/** Extracts an unquoted heredoc body from a role script. */
function heredoc(script: string, marker: string): string {
  const start = script.indexOf(marker);
  if (start < 0) throw new Error(`${marker} was not found`);
  const contentStart = start + marker.length;
  const end = script.indexOf("\nEOF", contentStart);
  if (end < 0) throw new Error(`${marker} is not terminated`);
  return script.slice(contentStart, end);
}

const template = synthesize(LONGEST_CLUSTER);
/** The desktop stack with the container flag on, where the broker task is built. */
const desktop = synthVdcWithEcs();

after(() => {
  for (const workdir of workdirs) rmSync(workdir, { force: true, recursive: true });
  cleanupWorkdirs();
});

/**
 * Every target group the container control plane creates, with the module id whose stack creates
 * it. The suffix is a hash of `<cluster>.<module-id>`, so each module's groups carry its own.
 * That the stacks really use these identifiers is pinned in `observability.test.ts`; what this
 * measures is the worst case a cluster name can put them in.
 */
const PLANNED_TARGET_GROUPS: ReadonlyArray<readonly [string, string]> = [
  ["cluster-manager", "cm-ecs-e"],
  ["cluster-manager", "cm-ecs-i"],
  ["cluster-manager", "cm-ecs-w"],
  ["scheduler", "sched-ecs-e"],
  ["scheduler", "sched-ecs-i"],
  ["vdc", "vdc-ecs-e"],
  ["vdc", "vdc-ecs-i"],
  ["vdc", "brk-ecs-c"],
  ["vdc", "brk-ecs-a"],
  ["vdc", "brk-ecs-g"],
  ["vdc", "gw-ecs-TN"],
  ["vdc", "gw-ecs-TUN"],
];

test("every target group name fits the longest cluster name a cluster can have", () => {
  // An identifier that is one character too long synthesizes on a short cluster name and throws on
  // a longer one, which makes the second cluster of a cutover the place it is found.
  const names = PLANNED_TARGET_GROUPS.map(([moduleId, identifier]) =>
    getTargetGroupName(LONGEST_CLUSTER, moduleId, identifier),
  );

  assert.equal(names.length, 12, "IP target groups");
  for (const name of names) {
    assert.ok(name.length <= TARGET_GROUP_NAME_LIMIT, `${name} is ${name.length} characters`);
    assert.ok(name.startsWith(`${LONGEST_CLUSTER}-`), name);
  }
  assert.equal(new Set(names).size, names.length, "target group names are distinct");

  // The container stack creates none of them: a service may not name a target group with no load
  // balancer, and it owns no listener.
  assert.equal(byType(template, "AWS::ElasticLoadBalancingV2::TargetGroup").length, 0);
});

test("the broker single address family option reaches the process that starts the broker", async () => {
  const brokerEnvironment = roleEnvironment(await desktop, "dcv-broker");
  // JAVA_TOOL_OPTIONS is read by the virtual machine itself, so no launcher has to cooperate.
  assert.equal(brokerEnvironment["JAVA_TOOL_OPTIONS"], PREFER_IPV4);

  const directory = mkdtempSync(join(tmpdir(), "ideactl-broker-role-"));
  workdirs.push(directory);
  const observed = join(directory, "launcher.env");
  stub(directory, "aws", "echo None");
  stub(directory, "getent", 'echo "192.0.2.10 $2"');
  stub(directory, "chown", "exit 0");
  stub(directory, "dcv-session-manager-broker", "exit 0");
  // Stands in for the privilege drop: records the command it would have run and the environment
  // that command would have started with.
  stub(
    directory,
    "setpriv",
    [
      'arguments=("$@")',
      'while [[ "${1-}" == --* ]]; do shift; done',
      `printf '%s\\n' "argv=\${arguments[*]}" "launcher=$1" "JAVA_TOOL_OPTIONS=\${JAVA_TOOL_OPTIONS-unset}" > ${observed}`,
    ].join("\n"),
  );

  execFileSync("bash", [join(IMAGE_ROLES, "broker.sh")], {
    encoding: "utf8",
    env: {
      ...brokerEnvironment,
      IDEA_BROKER_CONF_FILE: join(directory, "session-manager-broker.properties"),
      PATH: `${directory}:${process.env["PATH"] ?? ""}`,
    },
    // Bounded so a wedged role script cannot hang the suite, and high enough that a loaded
    // machine reports the script's own failure rather than this timeout.
    timeout: 120_000,
  });

  const lines = readFileSync(observed, "utf8").trim().split("\n");
  assert.equal(lines[2], `JAVA_TOOL_OPTIONS=${PREFER_IPV4}`, "the broker process starts with the option");
  assert.match(lines[1] ?? "", /dcv-session-manager-broker\.sh$/, "the vendor launcher is the command");
  assert.doesNotMatch(lines[0] ?? "", /--reset-env|env -i/, "nothing between the task and the broker clears the environment");
});

test("a virtual machine turns the broker option into the property it needs", { skip: javaSkip }, async () => {
  // The one hop the stack cannot prove by itself: that the variable the task definition sets is
  // read by the runtime rather than by a script that may or may not pass it on. The value has to
  // come from the synthesized broker task, or this only proves that a local JVM reads a constant.
  if (JAVA === undefined) requiredService("a Java runtime", JAVA_SETUP);
  const brokerOption = roleEnvironment(await desktop, "dcv-broker")["JAVA_TOOL_OPTIONS"];
  const env = { ...process.env };
  if (brokerOption === undefined) {
    delete env["JAVA_TOOL_OPTIONS"];
  } else {
    env["JAVA_TOOL_OPTIONS"] = brokerOption;
  }
  // -XshowSettings and -version both write to standard error, so both streams are read.
  const probe = spawnSync(JAVA, ["-XshowSettings:properties", "-version"], {
    encoding: "utf8",
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  assert.equal(probe.status, 0, `${probe.stdout}${probe.stderr}`);
  assert.match(`${probe.stdout}${probe.stderr}`, /java\.net\.preferIPv4Stack = true/);
});

test("the gateway binds one address family on every port including its health port", () => {
  const script = readFileSync(join(IMAGE_ROLES, "gateway.sh"), "utf8");
  const rendered = execFileSync(
    "bash",
    [
      "-c",
      `cat <<EOF\n${heredoc(script, "cat > /etc/dcv-connection-gateway/dcv-connection-gateway.conf <<EOF\n")}\nEOF`,
    ],
    {
      encoding: "utf8",
      env: {
        ...process.env,
        BROKER_PORT: "8446",
        CERTS: "/tmp/gateway-certs",
        DCV_GATEWAY_LOG_LEVEL: "info",
        IDEA_INTERNAL_ALB_ENDPOINT: "https://internal.example.invalid",
      },
    },
  );

  // The task network namespace has one address family, so a listener on the other one never comes
  // up, and the health port is the one the load balancer decides on.
  const endpoints = [...rendered.matchAll(/^(?:quic|web)-listen-endpoints = \[(.*)\]$/gm)].flatMap((match) =>
    (match[1] ?? "").split(",").map((entry) => entry.trim().replace(/^"|"$/g, "")),
  );
  assert.ok(endpoints.length >= 2, "the gateway declares its listeners");
  for (const endpoint of endpoints) {
    const address = endpoint.slice(0, endpoint.lastIndexOf(":"));
    assert.ok(isIPv4(address), `${endpoint} is not an IPv4 endpoint`);
  }

  const health = /^bind-addr = "(.*)"$/m.exec(rendered);
  assert.ok(health !== null, "the health check declares a bind address");
  assert.ok(isIPv4(health[1] ?? ""), `health check binds ${health[1]}`);
});

test("the host image and every task follow the configured family's architecture", async () => {
  // Hosts and tasks cannot disagree by configuration because both are derived from one resolved
  // architecture, and this is what proves the derivation rather than a runtime check comparing a
  // value with itself. Both real clusters run Intel families for their current control planes, so
  // the other family is an override an operator will reach for.
  const expected = [
    { instanceType: "m7g.large", parameter: "/aws/service/ecs/optimized-ami/amazon-linux-2023/arm64/recommended/image_id", cpu: "ARM64" },
    { instanceType: "m7i.large", parameter: "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id", cpu: "X86_64" },
  ];

  for (const { instanceType, parameter, cpu } of expected) {
    const template = synthesize(LONGEST_CLUSTER, { "ecs.hosts.instance_type": instanceType });
    const launchTemplates = byType(template, "AWS::EC2::LaunchTemplate");
    assert.equal(launchTemplates.length, 1, `${instanceType} launch template`);
    const launchData = record(
      record(launchTemplates[0]?.["Properties"], "launch template properties")["LaunchTemplateData"],
      "launch data",
    );
    assert.equal(launchData["InstanceType"], instanceType);
    assert.equal(launchData["ImageId"], `resolve:ssm:${parameter}`, `${instanceType} host image`);

    // The module stacks build their tasks from this published value rather than resolving the
    // family a second time, so no two stacks can disagree about the architecture.
    const settings = byType(template, "Custom::ClusterSettings")
      .map((resource) => record(resource["Properties"], "settings properties")["settings"])
      .map((value) => record(value, "published settings"));
    assert.equal(settings.length, 1, "the stack publishes one settings resource");
    assert.equal(settings[0]?.["cpu_architecture"], cpu, `${instanceType} published architecture`);

    // And a module stack stamps what it was given, so a change of family reaches the tasks.
    const moduleTemplate = await synthVdcWithEcs({ "ecs.cpu_architecture": { S: cpu } });
    const taskDefinitions = byType(moduleTemplate as JsonObject, "AWS::ECS::TaskDefinition");
    assert.equal(taskDefinitions.length, 3, `${instanceType} desktop task definitions`);
    for (const taskDefinition of taskDefinitions) {
      assert.deepEqual(
        record(taskDefinition["Properties"], "task definition properties")["RuntimePlatform"],
        { CpuArchitecture: cpu, OperatingSystemFamily: "LINUX" },
        `${instanceType} task platform`,
      );
    }
  }
});

test("a host instance family whose architecture cannot be resolved is refused", () => {
  // A family with no size resolves to no architecture, so the launch
  // template would carry an image for a guess and its hosts would never register.
  assert.throws(
    () => synthesize(LONGEST_CLUSTER, { "ecs.hosts.instance_type": "m7g" }),
    (error: unknown) =>
      error instanceof Error &&
      error.message.includes("ecs.hosts.instance_type m7g is not an instance type"),
  );
});
