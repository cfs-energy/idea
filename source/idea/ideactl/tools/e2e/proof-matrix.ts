#!/usr/bin/env node

import { spawn } from "node:child_process";
import { connect } from "node:tls";
import { IdeaApiClient } from "./api.ts";
import { apiLoadCheck } from "./checks/api-load.ts";
import { brokerTaskKillCheck } from "./checks/broker-task-kill.ts";
import { desktopEndToEndCheck } from "./checks/desktop-end-to-end.ts";
import { gatewayLoadCheck } from "./checks/gateway-load.ts";
import { gatewayTaskKillCheck } from "./checks/gateway-task-kill.ts";
import { jobBurstCheck } from "./checks/job-burst.ts";
import { schedulerReplacementCheck } from "./checks/scheduler-replacement.ts";
import type {
  ApiClient,
  CheckContext,
  CheckName,
  CheckResult,
  CloudClient,
  GatewayConnection,
  GatewayConnector,
  ProcessResult,
  ProcessRunner,
  ProofCheck,
  ProofMatrixOptions,
} from "./checks/types.ts";
import { CHECK_NAMES } from "./checks/types.ts";

const CHECKS: ProofCheck[] = [
  desktopEndToEndCheck,
  gatewayTaskKillCheck,
  brokerTaskKillCheck,
  schedulerReplacementCheck,
  jobBurstCheck,
  apiLoadCheck,
  gatewayLoadCheck,
];

interface ProofMatrixDependencies {
  api: ApiClient;
  cloud: CloudClient;
  gateway: GatewayConnector;
  now(): number;
  output(line: string): void;
  processes: ProcessRunner;
  sleep(milliseconds: number): Promise<void>;
}

interface ProofMatrixRun {
  exitCode: 0 | 1;
  results: Array<{ name: CheckName; result: CheckResult }>;
}

type RuntimeModule = Record<string, unknown>;

interface RuntimeClient {
  send(command: object): Promise<unknown>;
}

interface RuntimeClientConstructor {
  new (options: { region: string }): RuntimeClient;
}

interface RuntimeCommandConstructor {
  new (input: Record<string, string | string[]>): object;
}

/** Parses proof-matrix flags and their E2E environment variable equivalents. */
export function parseProofMatrixOptions(argv: string[], environment: NodeJS.ProcessEnv = process.env): ProofMatrixOptions {
  const values = new Map<string, string | boolean>();
  const selected: CheckName[] = [];
  const valueFlags = new Set([
    "alb-host",
    "api-max-error-count",
    "api-max-p95-ms",
    "api-rps",
    "api-seconds",
    "api-workers",
    "broker-service",
    "broker-target-group",
    "broker-task",
    "check",
    "cluster",
    "desktop-request",
    "expected-exit-status",
    "gateway-connections",
    "gateway-hold-seconds",
    "gateway-host",
    "gateway-max-failures",
    "gateway-max-p95-ms",
    "gateway-port",
    "gateway-ramp-seconds",
    "gateway-service",
    "gateway-target-group",
    "gateway-task",
    "job-count",
    "job-sleep-seconds",
    "password-file",
    "poll-seconds",
    "ready-timeout-seconds",
    "region",
    "replacement-timeout-seconds",
    "scheduler-service",
    "scheduler-task",
    "token-dir",
    "username",
  ]);

  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index];
    if (!flag.startsWith("--")) {
      throw new Error(`unexpected argument: ${flag}`);
    }
    const name = flag.slice(2);
    if (name === "insecure") {
      values.set(name, true);
      continue;
    }
    if (name === "help") {
      values.set(name, true);
      continue;
    }
    if (!valueFlags.has(name)) {
      throw new Error(`unknown flag: --${name}`);
    }
    const value = argv[index + 1];
    if (value === undefined || value.startsWith("--")) {
      throw new Error(`missing value for --${name}`);
    }
    if (name === "check") {
      for (const item of value.split(",")) {
        if (!isCheckName(item)) {
          throw new Error(`unknown check: ${item}`);
        }
        if (!selected.includes(item)) {
          selected.push(item);
        }
      }
    } else {
      values.set(name, value);
    }
    index += 1;
  }

  const read = (flag: string): string | undefined => {
    const supplied = values.get(flag);
    if (typeof supplied === "string") {
      return supplied;
    }
    return environment[`IDEA_E2E_${flag.replaceAll("-", "_").toUpperCase()}`];
  };
  const desktopRequest = read("desktop-request");
  const parsedDesktopRequest = desktopRequest === undefined ? undefined : parseJsonObject(desktopRequest, "--desktop-request");

  return {
    albHost: read("alb-host"),
    apiMaxErrorCount: optionalInteger(read("api-max-error-count"), "api-max-error-count"),
    apiMaxP95Ms: optionalPositiveNumber(read("api-max-p95-ms"), "api-max-p95-ms"),
    apiRps: optionalPositiveNumber(read("api-rps"), "api-rps"),
    apiSeconds: optionalPositiveNumber(read("api-seconds"), "api-seconds"),
    apiWorkers: optionalPositiveInteger(read("api-workers"), "api-workers"),
    brokerService: read("broker-service"),
    brokerTargetGroup: read("broker-target-group"),
    brokerTask: read("broker-task"),
    checks: selected.length === 0 ? [...CHECK_NAMES] : selected,
    cluster: read("cluster"),
    desktopRequest: parsedDesktopRequest,
    expectedExitStatus: optionalInteger(read("expected-exit-status"), "expected-exit-status"),
    gatewayConnections: optionalPositiveInteger(read("gateway-connections"), "gateway-connections"),
    gatewayHoldSeconds: optionalPositiveNumber(read("gateway-hold-seconds"), "gateway-hold-seconds"),
    gatewayHost: read("gateway-host"),
    gatewayMaxFailures: optionalInteger(read("gateway-max-failures"), "gateway-max-failures"),
    gatewayMaxP95Ms: optionalPositiveNumber(read("gateway-max-p95-ms"), "gateway-max-p95-ms"),
    gatewayPort: optionalPositiveInteger(read("gateway-port"), "gateway-port"),
    gatewayRampSeconds: optionalPositiveNumber(read("gateway-ramp-seconds"), "gateway-ramp-seconds"),
    gatewayService: read("gateway-service"),
    gatewayTargetGroup: read("gateway-target-group"),
    gatewayTask: read("gateway-task"),
    insecureTls: values.get("insecure") === true || environment.IDEA_E2E_INSECURE === "true",
    jobCount: optionalPositiveInteger(read("job-count"), "job-count"),
    jobSleepSeconds: optionalPositiveNumber(read("job-sleep-seconds"), "job-sleep-seconds"),
    passwordFile: read("password-file"),
    pollSeconds: optionalPositiveNumber(read("poll-seconds"), "poll-seconds"),
    readyTimeoutSeconds: optionalPositiveNumber(read("ready-timeout-seconds"), "ready-timeout-seconds"),
    region: read("region"),
    replacementTimeoutSeconds: optionalPositiveNumber(read("replacement-timeout-seconds"), "replacement-timeout-seconds"),
    schedulerService: read("scheduler-service"),
    schedulerTask: read("scheduler-task"),
    tokenDirectory: read("token-dir"),
    username: read("username"),
  };
}

/** Returns the required flags for the selected checks, without duplicates. */
export function missingRequiredFlags(options: ProofMatrixOptions): string[] {
  return CHECKS
    .filter((check) => options.checks.includes(check.name))
    .flatMap((check) => check.requiredFlags(options))
    .filter((flag, index, values) => values.indexOf(flag) === index);
}

/** Runs selected checks, prints their observations, and aggregates their status. */
export async function runProofMatrix(
  options: ProofMatrixOptions,
  dependencies: ProofMatrixDependencies,
): Promise<ProofMatrixRun> {
  const results: Array<{ name: CheckName; result: CheckResult }> = [];
  for (const check of CHECKS.filter((candidate) => options.checks.includes(candidate.name))) {
    dependencies.output(`CHECK ${check.name}: ${check.description}`);
    let result: CheckResult;
    try {
      const context: CheckContext = { ...dependencies, options };
      result = await check.run(context);
    } catch (error: unknown) {
      result = {
        observed: [error instanceof Error ? error.message : String(error)],
        passed: false,
      };
    }
    for (const observation of result.observed) {
      dependencies.output(`OBSERVED ${check.name}: ${observation}`);
    }
    dependencies.output(`${result.passed ? "PASS" : "FAIL"} ${check.name}`);
    results.push({ name: check.name, result });
  }
  return { exitCode: results.every(({ result }) => result.passed) ? 0 : 1, results };
}

/** Creates live adapters only after required flags have been validated. */
export function createLiveDependencies(options: ProofMatrixOptions): ProofMatrixDependencies {
  const api = new IdeaApiClient({
    albHost: requiredString(options.albHost, "alb-host"),
    username: requiredString(options.username, "username"),
    passwordFile: requiredString(options.passwordFile, "password-file"),
    tokenDirectory: options.tokenDirectory,
    insecureTls: options.insecureTls,
  });
  return {
    api,
    cloud: new LiveCloudClient(options.region),
    gateway: new TlsGatewayConnector(),
    now: () => Date.now(),
    output: (line) => console.log(line),
    processes: new NodeProcessRunner(),
    sleep: (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
  };
}

/** Prints command usage, check names, and all supported flags. */
export function usage(): string {
  return [
    "Usage: node tools/e2e/proof-matrix.ts [--check <name[,name]>] [options]",
    "",
    "Control flags:",
    "  --check <name[,name]>               Run one or more named checks, repeatable",
    "  --help                              Print this help text",
    "",
    "Checks:",
    ...CHECKS.map((check) => `  ${check.name}  ${check.description}`),
    "",
    "Connection flags:",
    "  --alb-host <host>                    IDEA_E2E_ALB_HOST",
    "  --username <name>                    IDEA_E2E_USERNAME",
    "  --password-file <path>               IDEA_E2E_PASSWORD_FILE",
    "  --token-dir <path>                   IDEA_E2E_TOKEN_DIR",
    "  --insecure                           IDEA_E2E_INSECURE=true",
    "  --gateway-host <host>                IDEA_E2E_GATEWAY_HOST",
    "  --gateway-port <number>              IDEA_E2E_GATEWAY_PORT, default 443",
    "  --desktop-request <json>             IDEA_E2E_DESKTOP_REQUEST",
    "",
    "ECS replacement flags:",
    "  --cluster <name>                     IDEA_E2E_CLUSTER",
    "  --region <region>                    IDEA_E2E_REGION",
    "  --gateway-service <name>             IDEA_E2E_GATEWAY_SERVICE",
    "  --gateway-task <id>                  IDEA_E2E_GATEWAY_TASK",
    "  --gateway-target-group <id>          IDEA_E2E_GATEWAY_TARGET_GROUP",
    "  --broker-service <name>              IDEA_E2E_BROKER_SERVICE",
    "  --broker-task <id>                   IDEA_E2E_BROKER_TASK",
    "  --broker-target-group <id>           IDEA_E2E_BROKER_TARGET_GROUP",
    "  --scheduler-service <name>           IDEA_E2E_SCHEDULER_SERVICE",
    "  --scheduler-task <id>                IDEA_E2E_SCHEDULER_TASK",
    "",
    "Timing and job flags:",
    "  --poll-seconds <number>              IDEA_E2E_POLL_SECONDS, default 10",
    "  --ready-timeout-seconds <number>     IDEA_E2E_READY_TIMEOUT_SECONDS, default 1800",
    "  --replacement-timeout-seconds <n>    IDEA_E2E_REPLACEMENT_TIMEOUT_SECONDS, default 900",
    "  --expected-exit-status <number>      IDEA_E2E_EXPECTED_EXIT_STATUS, default 0",
    "  --job-count <number>                 IDEA_E2E_JOB_COUNT, default 12",
    "  --job-sleep-seconds <number>         IDEA_E2E_JOB_SLEEP_SECONDS, default 120",
    "",
    "API load flags:",
    "  --api-rps <number>                   IDEA_E2E_API_RPS, default 50",
    "  --api-seconds <number>               IDEA_E2E_API_SECONDS, default 300",
    "  --api-workers <number>               IDEA_E2E_API_WORKERS, default 64",
    "  --api-max-p95-ms <number>            IDEA_E2E_API_MAX_P95_MS, required for api-load",
    "  --api-max-error-count <number>       IDEA_E2E_API_MAX_ERROR_COUNT, required for api-load",
    "",
    "Gateway load flags:",
    "  --gateway-connections <number>       IDEA_E2E_GATEWAY_CONNECTIONS, default 5000",
    "  --gateway-ramp-seconds <number>      IDEA_E2E_GATEWAY_RAMP_SECONDS, default 60",
    "  --gateway-hold-seconds <number>      IDEA_E2E_GATEWAY_HOLD_SECONDS, default 120",
    "  --gateway-max-p95-ms <number>        IDEA_E2E_GATEWAY_MAX_P95_MS, required for gateway-load",
    "  --gateway-max-failures <number>      IDEA_E2E_GATEWAY_MAX_FAILURES, required for gateway-load",
  ].join("\n");
}

/** Implements task and target-health operations through installed SDK modules. */
class LiveCloudClient implements CloudClient {
  private readonly region: string | undefined;

  public constructor(region: string | undefined) {
    this.region = region;
  }

  public async healthyTargetCount(targetGroup: string): Promise<number> {
    const response = await this.send("@aws-sdk/client-elastic-load-balancing-v2", "ElasticLoadBalancingV2Client", "DescribeTargetHealthCommand", {
      TargetGroupArn: targetGroup,
    });
    const descriptions = record(response)?.TargetHealthDescriptions;
    if (!Array.isArray(descriptions)) {
      return 0;
    }
    return descriptions.filter((description) => record(record(description)?.TargetHealth)?.State === "healthy").length;
  }

  public async serviceRunningCount(cluster: string, service: string): Promise<{ desired: number; running: number }> {
    const response = await this.send("@aws-sdk/client-ecs", "ECSClient", "DescribeServicesCommand", {
      cluster,
      services: [service],
    });
    const services = record(response)?.services;
    const serviceDescription = Array.isArray(services) ? services[0] : undefined;
    const description = record(serviceDescription);
    return {
      desired: numberValue(description?.desiredCount),
      running: numberValue(description?.runningCount),
    };
  }

  public async stopTask(cluster: string, task: string): Promise<void> {
    await this.send("@aws-sdk/client-ecs", "ECSClient", "StopTaskCommand", { cluster, task });
  }

  private async send(moduleName: string, clientName: string, commandName: string, input: Record<string, string | string[]>): Promise<unknown> {
    if (this.region === undefined || this.region.trim() === "") {
      throw new Error("missing required --region");
    }
    const sdk = await runtimeImport(moduleName);
    const Client = clientConstructor(sdk[clientName], clientName);
    const Command = commandConstructor(sdk[commandName], commandName);
    return new Client({ region: this.region }).send(new Command(input));
  }
}

/** Opens a TLS connection and keeps it available for task-replacement checks. */
class TlsGatewayConnector implements GatewayConnector {
  public async connect(host: string, port: number, insecureTls: boolean): Promise<GatewayConnection> {
    return new Promise((resolve, reject) => {
      const socket = connect({ host, port, rejectUnauthorized: !insecureTls, servername: host });
      const timer = setTimeout(() => finish(new Error("gateway connection timed out")), 20_000);
      let settled = false;
      const finish = (error?: Error): void => {
        if (settled) {
          return;
        }
        settled = true;
        clearTimeout(timer);
        if (error === undefined) {
          resolve({ close: () => socket.destroy(), isOpen: () => !socket.destroyed });
        } else {
          socket.destroy();
          reject(error);
        }
      };
      socket.once("secureConnect", () => {
        socket.write(["GET / HTTP/1.1", `Host: ${host}`, "Connection: keep-alive", "", ""].join("\r\n"));
      });
      socket.once("data", () => finish());
      socket.once("error", (error) => finish(error));
    });
  }
}

/** Runs existing E2E tools without a shell and captures their final output. */
class NodeProcessRunner implements ProcessRunner {
  public async run(command: string, args: string[]): Promise<ProcessResult> {
    return new Promise((resolve, reject) => {
      const child = spawn(command, args, { shell: false });
      const stdout: Buffer[] = [];
      const stderr: Buffer[] = [];
      child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
      child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));
      child.once("error", reject);
      child.once("close", (code) => resolve({
        exitCode: code ?? 1,
        stderr: Buffer.concat(stderr).toString("utf8"),
        stdout: Buffer.concat(stdout).toString("utf8"),
      }));
    });
  }
}

/** Dynamically imports an SDK client so the command can report a missing optional client clearly. */
async function runtimeImport(moduleName: string): Promise<RuntimeModule> {
  const importer = new Function("specifier", "return import(specifier)") as (specifier: string) => Promise<RuntimeModule>;
  try {
    return await importer(moduleName);
  } catch {
    throw new Error(`required SDK module is unavailable: ${moduleName}`);
  }
}

/** Validates that a dynamically imported SDK client is constructable. */
function clientConstructor(value: unknown, name: string): RuntimeClientConstructor {
  if (typeof value !== "function") {
    throw new Error(`SDK client is unavailable: ${name}`);
  }
  return value as RuntimeClientConstructor;
}

/** Validates that a dynamically imported SDK command is constructable. */
function commandConstructor(value: unknown, name: string): RuntimeCommandConstructor {
  if (typeof value !== "function") {
    throw new Error(`SDK command is unavailable: ${name}`);
  }
  return value as RuntimeCommandConstructor;
}

/** Converts object-shaped SDK responses without accepting arrays or null. */
function record(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

/** Converts a response count to a safe numeric value. */
function numberValue(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

/** Ensures JSON desktop-request values are objects. */
function parseJsonObject(value: string, flag: string): Record<string, never> | undefined {
  try {
    const parsed: unknown = JSON.parse(value);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      throw new Error("not an object");
    }
    return parsed as Record<string, never>;
  } catch {
    throw new Error(`${flag} must be a JSON object`);
  }
}

/** Validates an optional finite number. */
function optionalPositiveNumber(value: string | undefined, flag: string): number | undefined {
  if (value === undefined) {
    return undefined;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`--${flag} must be a positive number`);
  }
  return parsed;
}

/** Validates an optional positive integer. */
function optionalPositiveInteger(value: string | undefined, flag: string): number | undefined {
  const parsed = optionalPositiveNumber(value, flag);
  if (parsed !== undefined && !Number.isInteger(parsed)) {
    throw new Error(`--${flag} must be an integer`);
  }
  return parsed;
}

/** Validates an optional non-negative integer. */
function optionalInteger(value: string | undefined, flag: string): number | undefined {
  if (value === undefined) {
    return undefined;
  }
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 0) {
    throw new Error(`--${flag} must be a non-negative integer`);
  }
  return parsed;
}

/** Identifies check names supplied by the command line. */
function isCheckName(value: string): value is CheckName {
  return CHECK_NAMES.some((name) => name === value);
}

/** Rejects impossible live adapter construction after validation. */
function requiredString(value: string | undefined, flag: string): string {
  if (value === undefined || value.trim() === "") {
    throw new Error(`missing required --${flag}`);
  }
  return value;
}

/** Executes the command while preserving the documented exit-code contract. */
async function main(): Promise<void> {
  try {
    if (process.argv.slice(2).includes("--help")) {
      console.log(usage());
      return;
    }
    const options = parseProofMatrixOptions(process.argv.slice(2));
    const missing = missingRequiredFlags(options);
    if (missing.length > 0) {
      throw new Error(`missing required flags: ${missing.map((flag) => `--${flag}`).join(", ")}`);
    }
    const run = await runProofMatrix(options, createLiveDependencies(options));
    process.exitCode = run.exitCode;
  } catch (error: unknown) {
    console.error(error instanceof Error ? error.message : String(error));
    console.error(usage());
    process.exitCode = 2;
  }
}

if (import.meta.main) {
  await main();
}
