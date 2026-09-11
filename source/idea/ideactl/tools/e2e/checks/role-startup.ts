import { execFileSync, spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

export const ROLE_NAMES = [
  "cluster-manager",
  "vdc",
  "scheduler",
  "dcv-broker",
  "dcv-gateway",
] as const;

export type RoleName = (typeof ROLE_NAMES)[number];
export type RoleCondition = "no-configuration" | "minimum-environment";

export interface GatewayCredentials {
  certificate: string;
  privateKey: string;
}

export interface RoleStartupOptions {
  context: string;
  image: string;
  platform: string;
  timeoutMilliseconds: number;
}

export interface ContainerCommand {
  args: string[];
  containerName: string;
  environment: Readonly<Record<string, string>>;
  timeoutMilliseconds: number;
}

export interface ContainerResult {
  exitCode: number | null;
  signal: string | null;
  stderr: string;
  stdout: string;
  timedOut: boolean;
}

export interface ContainerRunner {
  run(command: ContainerCommand): Promise<ContainerResult>;
}

export interface RoleStartupObservation {
  condition: RoleCondition;
  findings: string[];
  firstExternalDependency: string;
  result: ContainerResult;
  role: RoleName;
}

/** Defines the process-owned state needed to start a role. */
export function roleEnvironment(
  role: RoleName,
  condition: RoleCondition,
  gatewayCredentials: GatewayCredentials,
): Record<string, string> {
  if (condition === "no-configuration") {
    return {
      AWS_EC2_METADATA_DISABLED: "true",
      IDEA_CONTAINER_ROLE: role,
    };
  }

  const common = {
    AWS_DEFAULT_REGION: "us-east-1",
    AWS_EC2_METADATA_DISABLED: "true",
    IDEA_CLUSTER_NAME: "idea-test1",
    IDEA_MODULE_SET: "default",
    IDEA_CONTAINER_ROLE: role,
  };
  switch (role) {
    case "cluster-manager":
      return { ...common, IDEA_MODULE_ID: "cluster-manager", IDEA_MODULE_NAME: "cluster-manager" };
    case "vdc":
      return { ...common, IDEA_MODULE_ID: "vdc", IDEA_MODULE_NAME: "virtual-desktop-controller" };
    case "scheduler":
      return {
        ...common,
        IDEA_MODULE_ID: "scheduler",
        IDEA_MODULE_NAME: "scheduler",
        IDEA_SCHEDULER_DNS_NAME: "scheduler.idea-test1.us-east-1.local",
        PBS_HOME: "/apps/idea-test1/pbs",
      };
    case "dcv-broker":
      return {
        ...common,
        IDEA_MODULE_ID: "vdc",
        IDEA_MODULE_NAME: "virtual-desktop-controller",
        IDEA_SERVICE_DISCOVERY_NAME: "example.invalid",
      };
    case "dcv-gateway":
      return {
        DCV_GATEWAY_CERT_PEM: gatewayCredentials.certificate,
        DCV_GATEWAY_KEY_PEM: gatewayCredentials.privateKey,
        IDEA_CONTAINER_ROLE: role,
        IDEA_INTERNAL_ALB_ENDPOINT: "https://example.invalid",
      };
  }
}

/** Identifies the first dependency outside the container for each role. */
export function firstExternalDependency(role: RoleName, condition: RoleCondition): string {
  switch (role) {
    case "cluster-manager":
    case "vdc":
      return "DynamoDB cluster-settings table, loaded while constructing the application context.";
    case "scheduler":
      return "DynamoDB accounts tables, scanned by user synchronization after PBS starts.";
    case "dcv-broker":
      return condition === "minimum-environment"
        ? "Service-discovery DNS for the broker peers, then DynamoDB cluster settings."
        : "DynamoDB cluster settings, read to obtain broker and identity settings.";
    case "dcv-gateway":
      return "The internal load balancer resolver, contacted when the gateway handles a session.";
  }
}

/**
 * True when the run stopped at the credential boundary, so it never reached the behaviour
 * the probe exists to observe.
 *
 * This probe deliberately passes no cloud credentials, so every role dies at its first cloud
 * call. That message is actionable in the narrow sense, which is why it appears in the
 * allowlist below, so a role failing for a completely different reason further in is
 * indistinguishable from one that had nowhere to authenticate, and a real permission defect
 * reads as no findings at all.
 *
 * So a run that stops here is inconclusive rather than clean, and the caller says so instead
 * of reporting a pass.
 */
export function stoppedAtCredentialBoundary(result: ContainerResult): boolean {
  return /Unable to locate credentials|ExpiredToken|InvalidClientTokenId/.test(
    `${result.stdout}\n${result.stderr}`,
  );
}

/** Classifies output that an operator cannot use to correct the startup failure. */
export function startupFindings(result: ContainerResult): string[] {
  const transcript = `${result.stdout}\n${result.stderr}`;
  const findings: string[] = [];
  if (result.timedOut) {
    findings.push("did not exit or report an unavailable dependency before the timeout");
  }
  if (/\bTraceback\b/.test(transcript)) {
    findings.push("prints a Python traceback instead of only an actionable startup error");
  }
  if (result.exitCode === 0 && !result.timedOut) {
    findings.push("exited successfully instead of remaining supervised after reporting startup");
  }
  if (
    result.exitCode !== 0
    && !result.timedOut
    && !/(is required|not set|Unable to locate credentials|could not determine|failed to initialize)/.test(transcript)
  ) {
    findings.push("failed without an actionable message naming the missing input or dependency");
  }
  return findings;
}

/** Runs both startup conditions for all image roles without passing cloud credentials. */
export async function probeRoleStartup(
  runner: ContainerRunner,
  options: RoleStartupOptions,
  gatewayCredentials: GatewayCredentials,
): Promise<RoleStartupObservation[]> {
  const observations: RoleStartupObservation[] = [];
  let sequence = 0;
  for (const role of ROLE_NAMES) {
    for (const condition of ["no-configuration", "minimum-environment"] as const) {
      const environment = roleEnvironment(role, condition, gatewayCredentials);
      const result = await runner.run({
        args: dockerArguments(options, environment, `role-startup-${process.pid}-${sequence}`),
        containerName: `role-startup-${process.pid}-${sequence}`,
        environment,
        timeoutMilliseconds: options.timeoutMilliseconds,
      });
      sequence += 1;
      observations.push({
        condition,
        findings: startupFindings(result),
        firstExternalDependency: firstExternalDependency(role, condition),
        result,
        role,
      });
    }
  }
  return observations;
}

/** Builds an argument vector that does not require a shell or cloud credentials. */
export function dockerArguments(
  options: Omit<RoleStartupOptions, "timeoutMilliseconds">,
  environment: Readonly<Record<string, string>>,
  containerName: string,
): string[] {
  const args = [
    "--context",
    options.context,
    "run",
    "--rm",
    "--name",
    containerName,
    "--platform",
    options.platform,
  ];
  for (const [name, value] of Object.entries(environment)) {
    args.push("--env", `${name}=${value}`);
  }
  args.push(options.image);
  return args;
}

/** Generates a short-lived self-signed key pair for the gateway configuration probe. */
export function createGatewayCredentials(): GatewayCredentials {
  const directory = mkdtempSync(join(tmpdir(), "ideactl-role-startup-"));
  const certificatePath = join(directory, "certificate.pem");
  const privateKeyPath = join(directory, "private-key.pem");
  try {
    execFileSync(
      "openssl",
      [
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-keyout",
        privateKeyPath,
        "-out",
        certificatePath,
        "-sha256",
        "-days",
        "1",
        "-nodes",
        "-subj",
        "/CN=example.invalid",
      ],
      { stdio: "pipe" },
    );
    return {
      certificate: readFileSync(certificatePath, "utf8"),
      privateKey: readFileSync(privateKeyPath, "utf8"),
    };
  } finally {
    rmSync(directory, { force: true, recursive: true });
  }
}

/** Executes a container and terminates its named instance when the deadline expires. */
export class LocalContainerRunner implements ContainerRunner {
  public async run(command: ContainerCommand): Promise<ContainerResult> {
    return new Promise((resolve) => {
      const child = spawn("docker", command.args, {
        env: { ...process.env, ...command.environment },
        shell: false,
        stdio: ["ignore", "pipe", "pipe"],
      });
      const stdout: Buffer[] = [];
      const stderr: Buffer[] = [];
      let timedOut = false;
      child.stdout?.on("data", (chunk: Buffer) => stdout.push(chunk));
      child.stderr?.on("data", (chunk: Buffer) => stderr.push(chunk));
      const timeout = setTimeout(() => {
        timedOut = true;
        const context = command.args[0] === "--context" ? command.args[1] : undefined;
        const killer = spawn(
          "docker",
          [...(context === undefined ? [] : ["--context", context]), "kill", command.containerName],
          { shell: false, stdio: "ignore" },
        );
        killer.once("error", () => undefined);
      }, command.timeoutMilliseconds);
      child.once("error", (error: Error) => {
        clearTimeout(timeout);
        resolve({
          exitCode: null,
          signal: null,
          stderr: error.message,
          stdout: Buffer.concat(stdout).toString("utf8"),
          timedOut,
        });
      });
      child.once("close", (exitCode: number | null, signal: NodeJS.Signals | null) => {
        clearTimeout(timeout);
        resolve({
          exitCode,
          signal,
          stderr: Buffer.concat(stderr).toString("utf8"),
          stdout: Buffer.concat(stdout).toString("utf8"),
          timedOut,
        });
      });
    });
  }
}

/** Prints all evidence without ever printing the generated certificate or private key. */
export function printObservations(observations: readonly RoleStartupObservation[]): void {
  for (const observation of observations) {
    const { condition, findings, firstExternalDependency, result, role } = observation;
    console.log(`${role} ${condition}: exit=${result.exitCode ?? "none"} timeout=${result.timedOut}`);
    console.log(`first external dependency: ${firstExternalDependency}`);
    console.log(result.stdout.trimEnd());
    if (result.stderr !== "") {
      console.error(result.stderr.trimEnd());
    }
    for (const finding of findings) {
      console.log(`FINDING: ${finding}`);
    }
  }
}

/** Parses only the local image probe options accepted by this standalone check. */
export function parseOptions(args: readonly string[]): RoleStartupOptions {
  const options: RoleStartupOptions = {
    context: "default",
    image: "idea-control-plane:v26.09.0",
    platform: "linux/arm64",
    timeoutMilliseconds: 15_000,
  };
  for (let index = 0; index < args.length; index += 1) {
    const option = args[index];
    const value = args[index + 1];
    if (option === "--context" && value !== undefined) {
      options.context = value;
      index += 1;
    } else if (option === "--image" && value !== undefined) {
      options.image = value;
      index += 1;
    } else if (option === "--platform" && value !== undefined) {
      options.platform = value;
      index += 1;
    } else if (option === "--timeout-seconds" && value !== undefined && /^\d+$/.test(value) && Number(value) > 0) {
      options.timeoutMilliseconds = Number(value) * 1_000;
      index += 1;
    } else {
      throw new Error(`usage: node tools/e2e/checks/role-startup.ts [--context value] [--image value] [--platform value] [--timeout-seconds positive-integer]`);
    }
  }
  return options;
}

/** Runs the local image probe and exits nonzero while it finds startup defects. */
async function main(): Promise<void> {
  const observations = await probeRoleStartup(
    new LocalContainerRunner(),
    parseOptions(process.argv.slice(2)),
    createGatewayCredentials(),
  );
  printObservations(observations);
  // Inconclusive runs are reported separately and do not count as passes: a probe that never
  // reached what it was checking has proved nothing, and saying so is the whole point.
  const inconclusive = observations.filter((observation) =>
    observation.findings.length === 0 && stoppedAtCredentialBoundary(observation.result),
  );
  for (const observation of inconclusive) {
    console.log(
      `INCONCLUSIVE ${observation.role} ${observation.condition}: stopped at the credential `
        + "boundary, so nothing past the first cloud call was observed",
    );
  }
  if (inconclusive.length > 0) {
    console.log(
      `${inconclusive.length} of ${observations.length} runs proved nothing. Run against an `
        + "enforcing endpoint to observe what the role does once it can authenticate.",
    );
  }
  if (observations.some((observation) => observation.findings.length > 0)) {
    process.exitCode = 1;
  }
}

if (process.argv[1]?.endsWith("/role-startup.ts")) {
  void main().catch((error: Error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
