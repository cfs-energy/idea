/**
 * Regression coverage for scheduler health and gateway resolver wiring.
 *
 * The tests synthesize the task definitions, execute the scheduler command
 * against a local Unix socket, and render the gateway role configuration.
 */

import assert from "node:assert/strict";
import { execFile, execFileSync } from "node:child_process";
import {
  chmodSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import { fileURLToPath } from "node:url";
import { App, Aws, Fn } from "aws-cdk-lib";

import { makeContext } from "../../src/cdk/constructs/base.ts";
import { EcsStack } from "../../src/cdk/stacks/ecs.ts";
import type { SynthReads } from "../../src/cdk/synth-reads.ts";
import { ClusterConfig } from "../../src/config/cluster-config.ts";
import { ideaVersion } from "../../src/version.ts";

type JsonObject = Record<string, unknown>;

interface ObservedRequest {
  readonly body: string;
  readonly contentType: string;
  readonly method: string;
  readonly path: string;
}

const ACCOUNT = "123456789012";
const CLUSTER = "idea-test1";
const REGION = "us-east-2";
const PACKAGE_ROOT = fileURLToPath(new URL("../../", import.meta.url));
/** Exact scheduler exec command the stack must synthesize. */
const SCHEDULER_HEALTH_COMMAND =
  "qstat -B && curl --fail --silent --show-error --unix-socket /run/idea.sock --max-time 4 --header 'Content-Type: application/json' --data '{\"header\":{\"namespace\":\"Scheduler.ListActiveJobs\"}}' http://localhost/scheduler/api/v1";
/** Exact assignment the gateway role script uses for the broker port. */
const GATEWAY_BROKER_PORT_ASSIGNMENT =
  "BROKER_PORT=\"${DCV_BROKER_GATEWAY_PORT:-8446}\"";
/** Exact resolver URL line inside the gateway configuration heredoc. */
const GATEWAY_RESOLVER_URL_TEMPLATE =
  "url = \"${IDEA_INTERNAL_ALB_ENDPOINT}:${BROKER_PORT}\"";
const GATEWAY_ROLE = join(
  PACKAGE_ROOT,
  "../../../deployment/ecr/idea-control-plane/roles/gateway.sh",
);
const workdirs: string[] = [];

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
function settings(): ClusterConfig {
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
    // The container stack attaches its own target groups to the listeners that serve them; every
    // one of these is a cluster-stack output.
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
    "cluster.route53.private_hosted_zone_id": "Z0123456789ABCDEF",
    "cm.iam_role_arn": syntheticArn(
      "iam",
      "role/synthetic-cluster-manager-role",
    ),
    "cm.security_group_id": "sg-0123456789abcdef2",
    "ecs.datadog.api_key_secret_arn": syntheticArn(
      "secretsmanager",
      "secret:synthetic-datadog-secret",
    ),
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
    "vdc.dcv_broker_role_arn": syntheticArn(
      "iam",
      "role/synthetic-broker-role",
    ),
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

/** Reports whether an unknown value is a JSON object. */
function isRecord(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Narrows an unknown synthesized value to a JSON object. */
function record(value: unknown, description: string): JsonObject {
  if (!isRecord(value)) throw new Error(description);
  return value;
}

/** Synthesizes the ECS stack without reading live services. */
function synthesize(): JsonObject {
  const outdir = mkdtempSync(join(tmpdir(), "ideactl-health-resolver-synth-"));
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
      config: settings(),
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
    "synthesized template must be an object",
  );
}

/** Finds the application container for one IDEA role. */
function roleContainer(template: JsonObject, role: string): JsonObject {
  const resources = record(
    template["Resources"],
    "template must contain resources",
  );
  for (const resourceValue of Object.values(resources)) {
    const resource = record(resourceValue, "resource must be an object");
    if (resource["Type"] !== "AWS::ECS::TaskDefinition") continue;
    const properties = record(
      resource["Properties"],
      "task definition must contain properties",
    );
    const containers = properties["ContainerDefinitions"];
    if (!Array.isArray(containers)) {
      throw new Error("task definition must contain container definitions");
    }
    for (const containerValue of containers) {
      const container = record(
        containerValue,
        "container definition must be an object",
      );
      const environment = container["Environment"];
      if (!Array.isArray(environment)) continue;
      const matchesRole = environment.some((entryValue) => {
        const entry = record(entryValue, "environment entry must be an object");
        return (
          entry["Name"] === "IDEA_CONTAINER_ROLE" &&
          entry["Value"] === role
        );
      });
      if (matchesRole) return container;
    }
  }
  throw new Error(`task definition for ${role} was not synthesized`);
}

/** Reads a required string from a synthesized container environment. */
function environmentValue(container: JsonObject, name: string): string {
  const environment = container["Environment"];
  if (!Array.isArray(environment)) {
    throw new Error("container must contain an environment");
  }
  for (const entryValue of environment) {
    const entry = record(entryValue, "environment entry must be an object");
    if (entry["Name"] !== name) continue;
    const value = entry["Value"];
    if (typeof value !== "string") {
      throw new Error(`${name} must be a string`);
    }
    return value;
  }
  throw new Error(`${name} was not synthesized`);
}

/** Reads the exact shell command from a synthesized container health check. */
function healthCommand(container: JsonObject): string {
  const healthCheck = record(
    container["HealthCheck"],
    "scheduler must contain a health check",
  );
  const command = healthCheck["Command"];
  if (
    !Array.isArray(command) ||
    command[0] !== "CMD-SHELL" ||
    typeof command[1] !== "string"
  ) {
    throw new Error("scheduler health check must contain a shell command");
  }
  return command[1];
}

/** Runs a shell health command and preserves failure diagnostics. */
function executeHealthCommand(
  command: string,
  environment: NodeJS.ProcessEnv,
): Promise<void> {
  return new Promise((resolve, reject) => {
    execFile(
      "bash",
      ["-c", command],
      {
        encoding: "utf8",
        env: environment,
        // Bounded so a wedged health check cannot hang the suite, and far enough
        // above the command's own `curl --max-time` that a loaded machine reports
        // the curl failure rather than a SIGTERM from this harness.
        timeout: 60_000,
      },
      (error, stdout, stderr) => {
        if (error !== null) {
          reject(
            new Error(
              `health command failed\nstdout:\n${stdout}\nstderr:\n${stderr}`,
              { cause: error },
            ),
          );
          return;
        }
        resolve();
      },
    );
  });
}

/** Starts an HTTP server on a Unix socket and records each request. */
async function startModuleSocket(
  socketPath: string,
  requests: ObservedRequest[],
): Promise<ReturnType<typeof createServer>> {
  const server = createServer((request, response) => {
    const chunks: Uint8Array[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => {
      requests.push({
        body: Buffer.concat(chunks).toString("utf8"),
        contentType: request.headers["content-type"] ?? "",
        method: request.method ?? "",
        path: request.url ?? "",
      });
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end("{\"success\":true}");
    });
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, resolve);
  });
  return server;
}

/** Closes a listening test server before its temporary directory is removed. */
function closeServer(
  server: ReturnType<typeof createServer>,
): Promise<void> {
  return new Promise((resolve, reject) => {
    server.close((error) => {
      if (error !== undefined) {
        reject(error);
        return;
      }
      resolve();
    });
  });
}

/** Extracts the unquoted gateway configuration heredoc from the role script. */
function gatewayConfigTemplate(script: string): string {
  const marker =
    "cat > /etc/dcv-connection-gateway/dcv-connection-gateway.conf <<EOF\n";
  const start = script.indexOf(marker);
  if (start < 0) throw new Error("gateway configuration heredoc was not found");
  const contentStart = start + marker.length;
  const end = script.indexOf("\nEOF", contentStart);
  if (end < 0) {
    throw new Error("gateway configuration heredoc is not terminated");
  }
  return script.slice(contentStart, end);
}

const template = synthesize();

after(() => {
  for (const workdir of workdirs) {
    rmSync(workdir, { force: true, recursive: true });
  }
});

test("scheduler health succeeds through the module Unix socket", async () => {
  const scheduler = roleContainer(template, "scheduler");
  const command = healthCommand(scheduler);
  assert.equal(command, SCHEDULER_HEALTH_COMMAND);
  const testDirectory = mkdtempSync(
    join(tmpdir(), "ideactl-scheduler-health-"),
  );
  const socketPath = join(testDirectory, "module.sock");
  const requests: ObservedRequest[] = [];
  const server = await startModuleSocket(socketPath, requests);

  try {
    const qstatPath = join(testDirectory, "qstat");
    const qstatArgsPath = join(testDirectory, "qstat.args");
    const curlPath = join(testDirectory, "curl");
    writeFileSync(
      qstatPath,
      [
        "#!/bin/bash",
        `printf '%s\\n' "$*" > ${JSON.stringify(qstatArgsPath)}`,
        "exit 0",
        "",
      ].join("\n"),
    );
    writeFileSync(
      curlPath,
      `#!/bin/bash
set -euo pipefail
arguments=()
for argument in "$@"; do
  if [[ "$argument" == "/run/idea.sock" ]]; then
    arguments+=("\${TEST_MODULE_SOCKET:?}")
  else
    arguments+=("$argument")
  fi
done
exec "\${REAL_CURL:?}" "\${arguments[@]}"
`,
    );
    chmodSync(qstatPath, 0o755);
    chmodSync(curlPath, 0o755);

    const realCurl = execFileSync(
      "bash",
      ["-c", "command -v curl"],
      { encoding: "utf8" },
    ).trim();
    assert.notEqual(realCurl, "", "curl must be available");
    await executeHealthCommand(command, {
      ...process.env,
      PATH: `${testDirectory}:${process.env["PATH"] ?? ""}`,
      REAL_CURL: realCurl,
      TEST_MODULE_SOCKET: socketPath,
    });

    assert.equal(readFileSync(qstatArgsPath, "utf8").trim(), "-B");
    assert.deepEqual(requests, [
      {
        body: "{\"header\":{\"namespace\":\"Scheduler.ListActiveJobs\"}}",
        contentType: "application/json",
        method: "POST",
        path: "/scheduler/api/v1",
      },
    ]);
  } finally {
    await closeServer(server);
    rmSync(testDirectory, { force: true, recursive: true });
  }
});

test("gateway resolver receives the stack endpoint with its scheme", () => {
  const gateway = roleContainer(template, "dcv-gateway");
  const endpoint = environmentValue(gateway, "IDEA_INTERNAL_ALB_ENDPOINT");
  assert.equal(endpoint, "https://internal.example.invalid");

  const roleScript = readFileSync(GATEWAY_ROLE, "utf8");
  const portAssignment =
    roleScript.match(/BROKER_PORT="\$\{DCV_BROKER_GATEWAY_PORT:-\d+\}"/)?.[0] ??
    "";
  assert.equal(portAssignment, GATEWAY_BROKER_PORT_ASSIGNMENT);
  const configTemplate = gatewayConfigTemplate(roleScript);
  const resolverUrl = configTemplate.split("\n").find((line) => {
    return line.includes("IDEA_INTERNAL_ALB_ENDPOINT");
  });
  assert.equal(resolverUrl, GATEWAY_RESOLVER_URL_TEMPLATE);
  const rendered = execFileSync(
    "bash",
    ["-c", `${portAssignment}\ncat <<EOF\n${configTemplate}\nEOF`],
    {
      encoding: "utf8",
      env: {
        ...process.env,
        CERTS: "/tmp/gateway-certs",
        DCV_GATEWAY_LOG_LEVEL: "info",
        IDEA_INTERNAL_ALB_ENDPOINT: endpoint,
      },
    },
  );

  assert.match(
    rendered,
    /\[resolver\]\nurl = "https:\/\/internal\.example\.invalid:8446"\ntls-strict = false/,
  );
});
