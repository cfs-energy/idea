#!/usr/bin/env node

import { IdeaApiClient, type JsonObject, type JsonValue } from "./api.ts";

export interface LatencyStats {
  count: number;
  maxMs: number;
  p50Ms: number;
  p95Ms: number;
  p99Ms: number;
}

interface LoadOptions {
  albHost: string;
  durationSeconds: number;
  insecureTls: boolean;
  passwordFile: string;
  rps: number;
  tokenDirectory?: string;
  username: string;
  workers: number;
}

interface ApiCall {
  namespace: string;
  payload: JsonValue;
}

/** Reduces latency samples to the percentile conventions used by the reference harness. */
export function latencyStats(samplesMs: number[]): LatencyStats {
  if (samplesMs.length === 0) {
    return { count: 0, maxMs: 0, p50Ms: 0, p95Ms: 0, p99Ms: 0 };
  }

  const sorted = [...samplesMs].sort((left, right) => left - right);
  const middle = sorted.length / 2;
  const p50Ms = sorted.length % 2 === 0
    ? (sorted[middle - 1] + sorted[middle]) / 2
    : sorted[Math.floor(middle)];

  return {
    count: sorted.length,
    maxMs: sorted[sorted.length - 1],
    p50Ms,
    p95Ms: sorted[Math.floor(sorted.length * 0.95)],
    p99Ms: sorted[Math.floor(sorted.length * 0.99)],
  };
}

async function runLoad(options: LoadOptions): Promise<void> {
  const client = new IdeaApiClient({
    albHost: options.albHost,
    username: options.username,
    passwordFile: options.passwordFile,
    tokenDirectory: options.tokenDirectory,
    insecureTls: options.insecureTls,
  });
  const projects = await client.request("Projects.ListProjects", {});
  const projectId = firstProjectId(projects.body);
  if (projects.status !== 200 || projectId === undefined) {
    throw new Error(`could not resolve a project_id: ${JSON.stringify(projects.body)}`);
  }

  // This is the same portal-session mix as the Python reference harness.
  const calls: ApiCall[] = [
    { namespace: "ClusterSettings.GetModuleSettings", payload: { module_id: "cluster-manager" } },
    { namespace: "Accounts.GetUser", payload: { username: options.username } },
    { namespace: "Projects.ListProjects", payload: {} },
    { namespace: "VirtualDesktop.ListSessions", payload: {} },
    { namespace: "VirtualDesktop.ListSoftwareStacks", payload: { project_id: projectId } },
    { namespace: "Scheduler.ListActiveJobs", payload: {} },
    { namespace: "SchedulerAdmin.ListQueueProfiles", payload: {} },
  ];

  const latencies: number[] = [];
  const errors = new Map<string, number>();
  const startedAt = performance.now();
  const stopAt = startedAt + options.durationSeconds * 1_000;
  const workerTasks = Array.from({ length: options.workers }, (_, worker) =>
    workerLoop(client, calls, worker, options.rps / options.workers, stopAt, latencies, errors),
  );

  let reported = 0;
  while (performance.now() < stopAt) {
    await sleep(Math.min(30_000, Math.max(0, stopAt - performance.now())));
    const window = latencies.slice(reported);
    reported = latencies.length;
    if (window.length > 0) {
      const stats = latencyStats(window);
      const elapsedSeconds = Math.min(options.durationSeconds, (performance.now() - startedAt) / 1_000);
      console.log(
        [
          `${clock()} total=${latencies.length} last30s=${stats.count} (${(stats.count / Math.min(30, elapsedSeconds)).toFixed(1)}/s)`,
          `p50=${stats.p50Ms.toFixed(0)}ms p95=${stats.p95Ms.toFixed(0)}ms p99=${stats.p99Ms.toFixed(0)}ms`,
          `max=${stats.maxMs.toFixed(0)}ms errors=${JSON.stringify(Object.fromEntries(errors))}`,
        ].join(" "),
      );
    }
  }

  await Promise.all(workerTasks);
  const stats = latencyStats(latencies);
  console.log(
    [
      `DONE requests=${stats.count} rate=${(stats.count / options.durationSeconds).toFixed(1)}/s`,
      `p50=${stats.p50Ms.toFixed(0)}ms p95=${stats.p95Ms.toFixed(0)}ms p99=${stats.p99Ms.toFixed(0)}ms`,
      `max=${stats.maxMs.toFixed(0)}ms errors=${JSON.stringify(Object.fromEntries(errors))}`,
    ].join(" "),
  );
}

async function workerLoop(
  client: IdeaApiClient,
  calls: ApiCall[],
  workerNumber: number,
  rps: number,
  stopAt: number,
  latencies: number[],
  errors: Map<string, number>,
): Promise<void> {
  const intervalMs = 1_000 / rps;
  let callNumber = workerNumber;
  let nextAt = performance.now();

  while (performance.now() < stopAt) {
    const call = calls[callNumber % calls.length];
    const startedAt = performance.now();
    try {
      const result = await client.request(call.namespace, call.payload);
      const success = result.status === 200 && asJsonObject(result.body)?.success === true;
      if (!success) {
        increment(errors, result.status === 200 ? "api-fail" : String(result.status));
      }
    } catch (error: unknown) {
      increment(errors, error instanceof Error ? error.constructor.name : "Error");
    }
    latencies.push(performance.now() - startedAt);
    callNumber += 1;
    nextAt += intervalMs;
    const remaining = nextAt - performance.now();
    if (remaining > 0) {
      await sleep(remaining);
    }
  }
}

function firstProjectId(body: JsonValue): string | undefined {
  const payload = asJsonObject(body)?.payload;
  const listing = payload === undefined ? undefined : asJsonObject(payload)?.listing;
  if (!Array.isArray(listing) || listing.length === 0) {
    return undefined;
  }
  const projectId = asJsonObject(listing[0])?.project_id;
  return typeof projectId === "string" ? projectId : undefined;
}

function asJsonObject(value: JsonValue): JsonObject | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value : undefined;
}

function increment(counts: Map<string, number>, key: string): void {
  counts.set(key, (counts.get(key) ?? 0) + 1);
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function clock(): string {
  return new Date().toISOString().slice(11, 19);
}

function usage(): string {
  return [
    "Usage: load-api.ts --alb-host <host> --username <user> --password-file <path> [options]",
    "",
    "Options:",
    "  --rps <number>       Total requests per second (default: 50)",
    "  --seconds <number>   Test duration in seconds (default: 300)",
    "  --workers <number>   Concurrent request loops (default: 64)",
    "  --token-dir <path>   Directory for cached tokens",
    "  --insecure           Disable TLS certificate verification",
  ].join("\n");
}

function parseOptions(argv: string[]): LoadOptions {
  const flags = parseFlags(argv);
  if (flags.get("help") === true) {
    console.log(usage());
    process.exit(0);
  }
  return {
    albHost: requiredString(flags, "alb-host"),
    durationSeconds: positiveNumber(flags, "seconds", 300),
    insecureTls: flags.get("insecure") === true,
    passwordFile: requiredString(flags, "password-file"),
    rps: positiveNumber(flags, "rps", 50),
    tokenDirectory: optionalString(flags, "token-dir"),
    username: requiredString(flags, "username"),
    workers: positiveInteger(flags, "workers", 64),
  };
}

function parseFlags(argv: string[]): Map<string, string | boolean> {
  const flags = new Map<string, string | boolean>();
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (!argument.startsWith("--")) {
      throw new Error(`unexpected argument: ${argument}`);
    }
    const name = argument.slice(2);
    if (name === "help" || name === "insecure") {
      flags.set(name, true);
      continue;
    }
    const value = argv[index + 1];
    if (value === undefined || value.startsWith("--")) {
      throw new Error(`missing value for --${name}`);
    }
    flags.set(name, value);
    index += 1;
  }
  return flags;
}

function requiredString(flags: Map<string, string | boolean>, name: string): string {
  const value = optionalString(flags, name);
  if (value === undefined) {
    throw new Error(`missing required --${name}`);
  }
  return value;
}

function optionalString(flags: Map<string, string | boolean>, name: string): string | undefined {
  const value = flags.get(name);
  return typeof value === "string" && value !== "" ? value : undefined;
}

function positiveNumber(flags: Map<string, string | boolean>, name: string, fallback: number): number {
  const value = optionalString(flags, name);
  if (value === undefined) {
    return fallback;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`--${name} must be a positive number`);
  }
  return parsed;
}

function positiveInteger(flags: Map<string, string | boolean>, name: string, fallback: number): number {
  const value = positiveNumber(flags, name, fallback);
  if (!Number.isInteger(value)) {
    throw new Error(`--${name} must be an integer`);
  }
  return value;
}

if (import.meta.main) {
  try {
    await runLoad(parseOptions(process.argv.slice(2)));
  } catch (error: unknown) {
    console.error(error instanceof Error ? error.message : String(error));
    console.error(usage());
    process.exitCode = 2;
  }
}
