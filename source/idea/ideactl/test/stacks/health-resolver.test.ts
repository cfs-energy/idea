/**
 * Regression coverage for scheduler health and gateway resolver wiring.
 *
 * The tests synthesize the task definitions, execute the scheduler command against a local Unix
 * socket, and render the gateway role configuration. Each task is built by the module stack that
 * publishes the settings its process reads, so the scheduler's container comes from a scheduler
 * synthesis and the gateway's from a desktop one.
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
import { requireCapture } from "../support/fixtures.ts";
import {
  CONFIG_FILE,
  CONTEXT_FILE,
  SYNTH_READS_FILE,
  cleanupWorkdirs,
  synthSchedulerWithEcs,
  synthVdcWithEcs,
} from "../support/ecs-harness.ts";

requireCapture(
  [CONFIG_FILE, SYNTH_READS_FILE, CONTEXT_FILE],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

/** The internal load balancer name the desktop synthesis is given, in place of the captured one. */
const INTERNAL_ALB_DNS_NAME = "internal.example.invalid";

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

/** Reports whether an unknown value is a JSON object. */
function isRecord(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Narrows an unknown synthesized value to a JSON object. */
function record(value: unknown, description: string): JsonObject {
  if (!isRecord(value)) throw new Error(description);
  return value;
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

const schedulerTemplate = synthSchedulerWithEcs();

after(() => {
  for (const workdir of workdirs) {
    rmSync(workdir, { force: true, recursive: true });
  }
  cleanupWorkdirs();
});

test("scheduler health succeeds through the module Unix socket", async () => {
  const scheduler = roleContainer((await schedulerTemplate) as JsonObject, "scheduler");
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

test("gateway resolver receives the stack endpoint with its scheme", async () => {
  // Overriding the load balancer name rather than reading the captured one keeps the expected
  // endpoint a literal, so a stack that read some other setting would not match it.
  const desktop = await synthVdcWithEcs({
    "cluster.load_balancers.internal_alb.load_balancer_dns_name": { S: INTERNAL_ALB_DNS_NAME },
  });
  const gateway = roleContainer(desktop as JsonObject, "dcv-gateway");
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
