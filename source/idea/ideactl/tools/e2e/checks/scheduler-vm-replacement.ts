#!/usr/bin/env node

import { randomUUID } from "node:crypto";

import { IdeaApiClient, type ApiCallResult, type JsonValue } from "../api.ts";
import {
  activeRunIdentity,
  completedJob,
  DEFAULT_WITNESS_DIRECTORY,
  numberValue,
  runIdentityFailures,
  runIdentityObservations,
  submitWitnessJob,
  unreportedRunFields,
  WITNESS_RERUN_EXIT_STATUS,
  type RunIdentity,
  type SubmittedJob,
} from "./job-survival.ts";
import {
  createSshRunner,
  optionalFlag,
  parseValueFlags,
  requiredFlag,
  runCommand,
  type CommandRunner,
  type RemoteRunner,
  type SshOptions,
} from "./scheduler-proving.ts";

/** The queue this check submits to. */
const QUEUE = "normal";

/** The scheduler API surface required by the virtual-machine replacement proof. */
export interface SchedulerApi {
  request(namespace: string, payload: JsonValue): Promise<ApiCallResult>;
}

/** All injected operations needed to run the virtual-machine replacement proof. */
export interface SchedulerVmReplacementDependencies {
  api: SchedulerApi;
  now(): number;
  remote: RemoteRunner;
  replacement: CommandRunner;
  sleep(milliseconds: number): Promise<void>;
}

/** Inputs for proving a long-running job survives a scheduler virtual-machine replacement. */
export interface SchedulerVmReplacementOptions {
  expectedExitStatus: number;
  jobSleepSeconds: number;
  pollSeconds: number;
  replacementArgs: readonly string[];
  replacementExecutable: string;
  schedulerHost: string;
  timeoutSeconds: number;
}

/** Evidence returned after attempting the scheduler virtual-machine replacement proof. */
export interface SchedulerVmReplacementResult {
  observed: string[];
  passed: boolean;
}

/**
 * Submits a witnessed job, proves it is running, invokes the supplied scheduler deployment
 * command, observes a new scheduler machine identity, and requires the original run to be the
 * same run afterwards.
 *
 * "The job finished with the expected exit status" is not that proof: a requeued job finishes
 * the same way from the same submission, having discarded the work already done. The witness
 * script's own rerun status is what tells the two apart, with the run's start time compared
 * as well wherever the job API reports it.
 */
export async function runSchedulerVmReplacementCheck(
  options: SchedulerVmReplacementOptions,
  dependencies: SchedulerVmReplacementDependencies,
): Promise<SchedulerVmReplacementResult> {
  const observed: string[] = [];
  const beforeIdentity = await schedulerMachineIdentity(options.schedulerHost, dependencies.remote);
  if (beforeIdentity === undefined) {
    return failed(observed, "scheduler_identity_before=unavailable");
  }
  observed.push(`scheduler_identity_before=${beforeIdentity}`);
  const queuesBefore = await schedulerQueueInventory(options.schedulerHost, dependencies.remote);
  if (queuesBefore === undefined) {
    return failed(observed, "queues_before=unavailable");
  }
  observed.push(`queues_before=${queuesBefore.join(",")}`);

  let job: SubmittedJob;
  try {
    job = await submitWitnessJob(dependencies.api, {
      identifier: `scheduler-vm-replacement-${randomUUID()}`,
      queue: QUEUE,
      sleepSeconds: options.jobSleepSeconds,
      witnessDirectory: DEFAULT_WITNESS_DIRECTORY,
    });
  } catch (error: unknown) {
    return failed(observed, `job_submission_error=${errorMessage(error)}`);
  }
  observed.push(`job=${job.jobId} submitted`);

  const before = await waitForRunningIdentity(job, options, dependencies);
  if (before === undefined) {
    return failed(observed, `job=${job.jobId} did_not_reach_a_running_state`);
  }
  const unreported = unreportedRunFields(before);
  if (unreported.length > 0) {
    observed.push(`not_compared=${unreported.join(",")} the job API did not report them for the active job`);
  }

  const replacement = await dependencies.replacement.run(options.replacementExecutable, options.replacementArgs);
  observed.push(`replacement_exit=${replacement.exitCode}`);
  if (replacement.exitCode !== 0) {
    return failed(observed, `replacement_stderr=${oneLine(replacement.stderr)}`);
  }

  const afterIdentity = await waitForNewSchedulerIdentity(beforeIdentity, options, dependencies);
  if (afterIdentity === undefined) {
    return failed(observed, "scheduler_identity_after=unchanged_or_unavailable");
  }
  observed.push(`scheduler_identity_after=${afterIdentity}`);

  const qstat = await dependencies.remote.run(
    options.schedulerHost,
    "/opt/pbs/bin/qstat -B >/dev/null 2>&1",
  );
  observed.push(`scheduler_qstat_exit=${qstat.exitCode}`);
  if (qstat.exitCode !== 0) {
    return failed(observed, `scheduler_qstat_stderr=${oneLine(qstat.stderr)}`);
  }
  const queuesAfter = await schedulerQueueInventory(options.schedulerHost, dependencies.remote);
  if (queuesAfter === undefined) {
    return failed(observed, "queues_after=unavailable");
  }
  observed.push(`queues_after=${queuesAfter.join(",")}`);
  if (queuesBefore.join("\n") !== queuesAfter.join("\n")) {
    return failed(observed, "queue_inventory_changed_across_replacement");
  }

  // The run identity is read before the completion wait: the job must still be the same run
  // now, while it is running, not merely finish cleanly later.
  const after = await activeRunIdentity(dependencies.api, job);
  observed.push(...runIdentityObservations(before, after));
  const identityFailures = runIdentityFailures(before, after);
  if (identityFailures.length > 0) {
    return failed(observed, ...identityFailures.map(oneLine));
  }

  const exitStatus = await waitForExitStatus(job, options, dependencies);
  if (exitStatus === undefined) {
    return failed(observed, `job=${job.jobUid} produced_no_completed_record`);
  }
  observed.push(`job=${job.jobUid} completed_exit_status=${exitStatus}`);
  if (exitStatus === WITNESS_RERUN_EXIT_STATUS && options.expectedExitStatus !== WITNESS_RERUN_EXIT_STATUS) {
    return failed(observed, "the job script ran a second time: the run was requeued, not carried across the replacement");
  }
  if (exitStatus !== options.expectedExitStatus) {
    return failed(observed, `expected_exit_status=${options.expectedExitStatus}`);
  }
  return { observed, passed: true };
}

/** Waits for the batch server to report the submitted job running. */
async function waitForRunningIdentity(
  job: SubmittedJob,
  options: SchedulerVmReplacementOptions,
  dependencies: SchedulerVmReplacementDependencies,
): Promise<RunIdentity | undefined> {
  let identity: RunIdentity | undefined;
  const running = await waitUntil(options.timeoutSeconds, options.pollSeconds, dependencies, async () => {
    identity = await activeRunIdentity(dependencies.api, job);
    return identity?.state === "RUNNING";
  });
  return running ? identity : undefined;
}

/** Polls the stable scheduler host until it resolves to a different virtual machine. */
async function waitForNewSchedulerIdentity(
  priorIdentity: string,
  options: SchedulerVmReplacementOptions,
  dependencies: SchedulerVmReplacementDependencies,
): Promise<string | undefined> {
  let currentIdentity: string | undefined;
  const replaced = await waitUntil(options.timeoutSeconds, options.pollSeconds, dependencies, async () => {
    currentIdentity = await schedulerMachineIdentity(options.schedulerHost, dependencies.remote);
    return currentIdentity !== undefined && currentIdentity !== priorIdentity;
  });
  return replaced ? currentIdentity : undefined;
}

/** Waits for the submitted job's persistent completed record and returns its exit status. */
async function waitForExitStatus(
  job: SubmittedJob,
  options: SchedulerVmReplacementOptions,
  dependencies: SchedulerVmReplacementDependencies,
): Promise<number | undefined> {
  let exitStatus: number | undefined;
  await waitUntil(options.timeoutSeconds, options.pollSeconds, dependencies, async () => {
    exitStatus = numberValue((await completedJob(dependencies.api, job))?.["exit_status"]);
    return exitStatus !== undefined;
  });
  return exitStatus;
}

/** Reads a scheduler machine's immutable local identity through the stable scheduler name. */
async function schedulerMachineIdentity(host: string, remote: RemoteRunner): Promise<string | undefined> {
  try {
    const result = await remote.run(host, "cat /etc/machine-id");
    if (result.exitCode !== 0) {
      return undefined;
    }
    const identity = result.stdout.trim();
    return /^[a-f0-9]{32}$/i.test(identity) ? identity : undefined;
  } catch {
    return undefined;
  }
}

/** Reads the scheduler's queue names through the stable scheduler hostname. */
async function schedulerQueueInventory(host: string, remote: RemoteRunner): Promise<string[] | undefined> {
  try {
    const result = await remote.run(host, '/opt/pbs/bin/qmgr -c "list queue"');
    if (result.exitCode !== 0) {
      return undefined;
    }
    const queues = result.stdout
      .split(/\r?\n/)
      .map((line) => /^Queue ([^\s]+)$/.exec(line.trim())?.[1])
      .filter((queue): queue is string => queue !== undefined)
      .sort();
    return queues;
  } catch {
    return undefined;
  }
}

/** Repeats an asynchronous observation until it passes or the deadline expires. */
async function waitUntil(
  timeoutSeconds: number,
  pollSeconds: number,
  dependencies: Pick<SchedulerVmReplacementDependencies, "now" | "sleep">,
  predicate: () => Promise<boolean>,
): Promise<boolean> {
  const deadline = dependencies.now() + timeoutSeconds * 1_000;
  while (dependencies.now() <= deadline) {
    try {
      if (await predicate()) {
        return true;
      }
    } catch {
      // The scheduler API is unavailable while the old host is gone and the new host starts.
    }
    await dependencies.sleep(pollSeconds * 1_000);
  }
  return false;
}

/** Appends observations to a failed result without losing the successful observations. */
function failed(observed: string[], ...more: string[]): SchedulerVmReplacementResult {
  return { observed: [...observed, ...more], passed: false };
}

/** Formats an unknown error without introducing multi-line evidence. */
function errorMessage(error: unknown): string {
  return error instanceof Error ? oneLine(error.message) : oneLine(String(error));
}

/** Converts process diagnostics to one evidence line. */
function oneLine(value: string): string {
  return value.replaceAll(/\s+/g, " ").trim();
}

/** Parses the standalone virtual-machine replacement check's command-line flags. */
export function parseSchedulerVmReplacementOptions(argv: readonly string[]): {
  api: { albHost: string; insecureTls: boolean; passwordFile: string; tokenDirectory?: string; username: string };
  check: SchedulerVmReplacementOptions;
  ssh: SshOptions;
} {
  const insecure = argv.includes("--insecure");
  const values = parseValueFlags(
    argv.filter((value) => value !== "--insecure"),
    new Set([
      "alb-host",
      "expected-exit-status",
      "job-sleep-seconds",
      "password-file",
      "poll-seconds",
      "replace-arg",
      "replace-executable",
      "scheduler-host",
      "ssh-identity-file",
      "ssh-option",
      "ssh-port",
      "ssh-user",
      "timeout-seconds",
      "token-dir",
      "username",
    ]),
    new Set(["replace-arg", "ssh-option"]),
    new Set(["replace-arg"]),
  );
  return {
    api: {
      albHost: requiredFlag(values, "alb-host"),
      insecureTls: insecure,
      passwordFile: requiredFlag(values, "password-file"),
      tokenDirectory: optionalFlag(values, "token-dir"),
      username: requiredFlag(values, "username"),
    },
    check: {
      expectedExitStatus: nonNegativeInteger(optionalFlag(values, "expected-exit-status") ?? "0", "expected-exit-status"),
      jobSleepSeconds: positiveInteger(requiredFlag(values, "job-sleep-seconds"), "job-sleep-seconds"),
      pollSeconds: positiveInteger(optionalFlag(values, "poll-seconds") ?? "10", "poll-seconds"),
      replacementArgs: values.get("replace-arg") ?? [],
      replacementExecutable: requiredFlag(values, "replace-executable"),
      schedulerHost: requiredFlag(values, "scheduler-host"),
      timeoutSeconds: positiveInteger(optionalFlag(values, "timeout-seconds") ?? "1800", "timeout-seconds"),
    },
    ssh: {
      identityFile: optionalFlag(values, "ssh-identity-file"),
      options: (values.get("ssh-option") ?? []).map(parseSshOption),
      port: optionalPort(optionalFlag(values, "ssh-port")),
      user: requiredFlag(values, "ssh-user"),
    },
  };
}

/** Validates an SSH option passed through to `ssh -o`. */
function parseSshOption(value: string): string {
  if (!/^[A-Za-z][A-Za-z0-9]*=.+$/.test(value) || /[\r\n]/.test(value)) {
    throw new Error("--ssh-option must use Name=Value");
  }
  return value;
}

/** Validates an optional SSH port. */
function optionalPort(value: string | undefined): number | undefined {
  if (value === undefined) {
    return undefined;
  }
  return positiveInteger(value, "ssh-port", 65_535);
}

/** Validates an integer flag within an inclusive range. */
function positiveInteger(value: string, flag: string, maximum = Number.MAX_SAFE_INTEGER): number {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > maximum) {
    throw new Error(`--${flag} must be an integer from 1 through ${maximum}`);
  }
  return parsed;
}

/** Validates a non-negative integer flag. */
function nonNegativeInteger(value: string, flag: string): number {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 0) {
    throw new Error(`--${flag} must be a non-negative integer`);
  }
  return parsed;
}

async function main(): Promise<void> {
  try {
    const { api: apiOptions, check, ssh } = parseSchedulerVmReplacementOptions(process.argv.slice(2));
    const result = await runSchedulerVmReplacementCheck(check, {
      api: new IdeaApiClient(apiOptions),
      now: () => Date.now(),
      remote: createSshRunner(ssh),
      replacement: { run: runCommand },
      sleep: (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
    });
    for (const observation of result.observed) {
      console.log(`OBSERVED ${observation}`);
    }
    console.log(`${result.passed ? "PASS" : "FAIL"} scheduler-vm-replacement`);
    process.exitCode = result.passed ? 0 : 1;
  } catch (error: unknown) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 2;
  }
}

if (import.meta.main) {
  await main();
}
