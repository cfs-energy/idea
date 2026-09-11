#!/usr/bin/env node

import { randomUUID } from "node:crypto";
import { IdeaApiClient, type JsonObject, type JsonValue } from "./api.ts";

interface JobLoadOptions {
  albHost: string;
  count: number;
  insecureTls: boolean;
  maxPolls: number;
  passwordFile: string;
  pollSeconds: number;
  tokenDirectory?: string;
  username: string;
}

interface CompletedJob {
  executionHosts: JsonValue[];
  exitStatus: JsonValue | undefined;
  jobId: string | undefined;
  totalTimeSecs: JsonValue | undefined;
}

/** Submits a short PBS burst and reports the same active and completed job data as the shell harness. */
async function runJobLoad(options: JobLoadOptions): Promise<void> {
  const client = new IdeaApiClient({
    albHost: options.albHost,
    username: options.username,
    passwordFile: options.passwordFile,
    tokenDirectory: options.tokenDirectory,
    insecureTls: options.insecureTls,
  });

  console.log(`${clock()} submitting ${options.count} jobs`);
  const jobIds: string[] = [];
  for (let index = 1; index <= options.count; index += 1) {
    const result = await client.request("Scheduler.SubmitJob", {
      job_script_interpreter: "pbs",
      job_script: Buffer.from(pbsScript(index), "utf8").toString("base64"),
      client_submission_id: `burst-${randomUUID()}`,
    });
    const jobId = submittedJobId(result.body);
    if (result.status !== 200 || jobId === undefined) {
      throw new Error(`job ${index} was not submitted: ${JSON.stringify(result.body)}`);
    }
    jobIds.push(jobId);
    process.stdout.write(`${jobId} `);
  }
  process.stdout.write("\n");
  console.log(`${clock()} submitted: ${jobIds.join(" ")}`);

  for (let poll = 1; poll <= options.maxPolls; poll += 1) {
    await sleep(options.pollSeconds * 1_000);
    const active = await client.request("Scheduler.ListActiveJobs", {});
    if (active.status !== 200) {
      throw new Error(`could not list active jobs: ${JSON.stringify(active.body)}`);
    }
    const states = burstStates(active.body);
    console.log(`${clock()} t=${poll * options.pollSeconds}s active=${JSON.stringify(states)}`);
    if (Object.keys(states).length === 0) {
      break;
    }
  }

  const completed = await client.request("Scheduler.ListCompletedJobs", { paginator: { page_size: 50 } });
  if (completed.status !== 200) {
    throw new Error(`could not list completed jobs: ${JSON.stringify(completed.body)}`);
  }
  const jobs = completedBurstJobs(completed.body);
  const succeeded = jobs.filter((job) => job.exitStatus === 0);
  console.log(`${clock()} finished records:`);
  console.log(`  burst jobs finished: ${jobs.length}  exit0: ${succeeded.length}`);
  for (const job of jobs.slice(0, 5)) {
    console.log(
      [
        "   ",
        job.jobId ?? "",
        job.exitStatus ?? "",
        "secs",
        job.totalTimeSecs ?? "",
        firstInstanceType(job.executionHosts) ?? "",
      ].join(" "),
    );
  }
}

function pbsScript(index: number): string {
  return [
    "#!/bin/bash",
    `#PBS -N burst-${index}`,
    "#PBS -q normal",
    "#PBS -P default",
    "uname -m; /bin/sleep 20",
    "",
  ].join("\n");
}

function submittedJobId(body: JsonValue): string | undefined {
  const payload = asJsonObject(body)?.payload;
  const job = payload === undefined ? undefined : asJsonObject(payload)?.job;
  const jobId = job === undefined ? undefined : asJsonObject(job)?.job_id;
  return typeof jobId === "string" ? jobId : undefined;
}

function burstStates(body: JsonValue): Record<string, number> {
  const listing = listingFrom(body);
  const states: Record<string, number> = {};
  for (const entry of listing) {
    const job = asJsonObject(entry);
    if (job?.name === undefined || !String(job.name).startsWith("burst-")) {
      continue;
    }
    const state = String(job.state);
    states[state] = (states[state] ?? 0) + 1;
  }
  return states;
}

function completedBurstJobs(body: JsonValue): CompletedJob[] {
  return listingFrom(body)
    .map(asJsonObject)
    .filter((job): job is JsonObject => job !== undefined && typeof job.name === "string" && job.name.startsWith("burst-"))
    .map((job) => ({
      executionHosts: Array.isArray(job.execution_hosts) ? job.execution_hosts : [],
      exitStatus: job.exit_status,
      jobId: typeof job.job_id === "string" ? job.job_id : undefined,
      totalTimeSecs: job.total_time_secs,
    }));
}

function listingFrom(body: JsonValue): JsonValue[] {
  const payload = asJsonObject(body)?.payload;
  const listing = payload === undefined ? undefined : asJsonObject(payload)?.listing;
  return Array.isArray(listing) ? listing : [];
}

function firstInstanceType(hosts: JsonValue[]): string | undefined {
  const instanceType = asJsonObject(hosts[0])?.instance_type;
  return typeof instanceType === "string" ? instanceType : undefined;
}

function asJsonObject(value: JsonValue): JsonObject | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value : undefined;
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function clock(): string {
  return new Date().toISOString().slice(11, 19);
}

function usage(): string {
  return [
    "Usage: load-jobs.ts --alb-host <host> --username <user> --password-file <path> [options]",
    "",
    "Options:",
    "  --count <count>          Jobs to submit (default: 12)",
    "  --poll-seconds <seconds> Active-job polling interval (default: 30)",
    "  --max-polls <count>      Maximum active-job polls (default: 60)",
    "  --token-dir <path>       Directory for cached tokens",
    "  --insecure               Disable TLS certificate verification",
  ].join("\n");
}

function parseOptions(argv: string[]): JobLoadOptions {
  const flags = parseFlags(argv);
  if (flags.get("help") === true) {
    console.log(usage());
    process.exit(0);
  }
  return {
    albHost: requiredString(flags, "alb-host"),
    count: positiveInteger(flags, "count", 12),
    insecureTls: flags.get("insecure") === true,
    maxPolls: positiveInteger(flags, "max-polls", 60),
    passwordFile: requiredString(flags, "password-file"),
    pollSeconds: positiveNumber(flags, "poll-seconds", 30),
    tokenDirectory: optionalString(flags, "token-dir"),
    username: requiredString(flags, "username"),
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
    await runJobLoad(parseOptions(process.argv.slice(2)));
  } catch (error: unknown) {
    console.error(error instanceof Error ? error.message : String(error));
    console.error(usage());
    process.exitCode = 2;
  }
}
