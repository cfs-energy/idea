import assert from "node:assert/strict";
import test from "node:test";
import {
  dockerArguments,
  firstExternalDependency,
  parseOptions,
  probeRoleStartup,
  roleEnvironment,
  startupFindings,
  stoppedAtCredentialBoundary,
} from "../../tools/e2e/checks/role-startup.ts";
import type {
  ContainerCommand,
  ContainerResult,
  ContainerRunner,
  GatewayCredentials,
  RoleStartupOptions,
} from "../../tools/e2e/checks/role-startup.ts";

const gatewayCredentials: GatewayCredentials = {
  certificate: "certificate",
  privateKey: "private-key",
};

const options: RoleStartupOptions = {
  context: "default",
  image: "idea-control-plane:v26.09.0",
  platform: "linux/arm64",
  timeoutMilliseconds: 1_000,
};

/** Records probe invocations and supplies a deterministic process result. */
class RecordingRunner implements ContainerRunner {
  public readonly commands: ContainerCommand[] = [];

  public async run(command: ContainerCommand): Promise<ContainerResult> {
    this.commands.push(command);
    return {
      exitCode: 1,
      signal: null,
      stderr: "",
      stdout: "IDEA_CLUSTER_NAME is required",
      timedOut: false,
    };
  }
}

test("supplies only the role selector without configuration", () => {
  assert.deepEqual(roleEnvironment("scheduler", "no-configuration", gatewayCredentials), {
    AWS_EC2_METADATA_DISABLED: "true",
    IDEA_CONTAINER_ROLE: "scheduler",
  });
});

test("uses a valid synthetic cluster name in each minimum environment", () => {
  const environment = roleEnvironment("scheduler", "minimum-environment", gatewayCredentials);
  assert.equal(environment.IDEA_CLUSTER_NAME, "idea-test1");
  assert.equal(environment.IDEA_SCHEDULER_DNS_NAME, "scheduler.idea-test1.us-east-1.local");
  assert.equal(environment.PBS_HOME, "/apps/idea-test1/pbs");
});

test("supplies generated gateway material only to the gateway probe", () => {
  const environment = roleEnvironment("dcv-gateway", "minimum-environment", gatewayCredentials);
  assert.equal(environment.DCV_GATEWAY_CERT_PEM, "certificate");
  assert.equal(environment.DCV_GATEWAY_KEY_PEM, "private-key");
  assert.equal(environment.IDEA_INTERNAL_ALB_ENDPOINT, "https://example.invalid");
});

test("builds a shell-free local container command", () => {
  const args = dockerArguments(
    options,
    { IDEA_CONTAINER_ROLE: "vdc" },
    "role-startup-test",
  );
  assert.deepEqual(args, [
    "--context",
    "default",
    "run",
    "--rm",
    "--name",
    "role-startup-test",
    "--platform",
    "linux/arm64",
    "--env",
    "IDEA_CONTAINER_ROLE=vdc",
    "idea-control-plane:v26.09.0",
  ]);
});

test("reports traces, timeouts, and premature success as findings", () => {
  assert.deepEqual(
    startupFindings({
      exitCode: 0,
      signal: null,
      stderr: "",
      stdout: "Traceback",
      timedOut: true,
    }),
    [
      "did not exit or report an unavailable dependency before the timeout",
      "prints a Python traceback instead of only an actionable startup error",
    ],
  );
});

test("probes every role in both conditions and records dependencies", async () => {
  const runner = new RecordingRunner();
  const observations = await probeRoleStartup(runner, options, gatewayCredentials);
  assert.equal(observations.length, 10);
  assert.equal(runner.commands.length, 10);
  assert.deepEqual(
    observations.map((observation) => `${observation.role}:${observation.condition}`),
    [
      "cluster-manager:no-configuration",
      "cluster-manager:minimum-environment",
      "vdc:no-configuration",
      "vdc:minimum-environment",
      "scheduler:no-configuration",
      "scheduler:minimum-environment",
      "dcv-broker:no-configuration",
      "dcv-broker:minimum-environment",
      "dcv-gateway:no-configuration",
      "dcv-gateway:minimum-environment",
    ],
  );
  assert.equal(
    firstExternalDependency("dcv-broker", "minimum-environment"),
    "Service-discovery DNS for the broker peers, then DynamoDB cluster settings.",
  );
});

test("rejects incomplete and invalid command options", () => {
  assert.throws(() => parseOptions(["--image"]));
  assert.throws(() => parseOptions(["--timeout-seconds", "0"]));
  assert.deepEqual(parseOptions(["--timeout-seconds", "3"]), {
    ...options,
    timeoutMilliseconds: 3_000,
  });
});

// The probe passes no credentials on purpose, so every role dies at its first cloud call and the
// message for that is on the actionable allowlist: a run that never reached a cloud call reports
// no findings and reads as a pass. These cases pin the distinction between a clean run and one
// that never reached what the probe exists to observe.
const resultWith = (stderr: string, exitCode = 1): ContainerResult => ({
  exitCode,
  signal: null,
  stderr,
  stdout: "",
  timedOut: false,
});

test("a run that stops at the credential boundary is not reported as clean", () => {
  const result = resultWith(
    "botocore.exceptions.NoCredentialsError: Unable to locate credentials",
  );
  // No finding, which is the trap: on its own that reads as a pass.
  assert.deepEqual(startupFindings(result), []);
  assert.equal(stoppedAtCredentialBoundary(result), true);
});

test("an expired or invalid session counts as the same boundary", () => {
  assert.equal(stoppedAtCredentialBoundary(resultWith("ExpiredToken: the token expired")), true);
  assert.equal(
    stoppedAtCredentialBoundary(resultWith("InvalidClientTokenId: no such key")),
    true,
  );
});

test("a genuine permission refusal is past the boundary and stays a real failure", () => {
  // Authenticated and then refused, so it is past the credential boundary.
  const refused = resultWith(
    "AccessDeniedException: User is not authorized to perform: dynamodb:Scan",
  );
  assert.equal(stoppedAtCredentialBoundary(refused), false);
  assert.deepEqual(startupFindings(refused), [
    "failed without an actionable message naming the missing input or dependency",
  ]);
});

test("a successful start is neither inconclusive nor a finding", () => {
  const started = resultWith("[entrypoint] starting scheduler module", 1);
  assert.equal(stoppedAtCredentialBoundary(started), false);
});
