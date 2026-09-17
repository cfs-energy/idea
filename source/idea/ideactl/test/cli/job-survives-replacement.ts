#!/usr/bin/env node

/**
 * The programme's acceptance test: a running batch job survives a control-plane replacement.
 *
 * The point of moving the control plane into containers is that an upgrade stops requiring a
 * drain, so the criterion is not that the job eventually finishes. A requeued job also finishes,
 * with the same exit status, from the same submission, having thrown away whatever work was
 * already done. This check therefore compares the identity of the *run*, not the fate of the job:
 *
 *   - the job script's own witness records exactly one execution, and its beats name one host,
 *   - the batch server's start time for the job is unchanged, so the run was never restarted, and
 *     its execution host and instance are unchanged, for whichever of the three the job API
 *     reported before the replacement,
 *   - the control-plane outage is shorter than the node-failure requeue window, so the server
 *     had no opportunity to requeue work that was running fine,
 *   - and only then, that the job finished cleanly.
 *
 * Each of those can fail on its own and each names a different cause. The witness is first because
 * it is the only one that needs no field the job API might not report: measured on the development
 * cluster, an active job carries neither a start time nor an execution host. Fields it did not
 * report are named as not compared, never compared as one absent value against another, and a
 * witness that could not be read is a failure rather than a run with no reruns.
 */

import { randomUUID } from "node:crypto";

import { IdeaApiClient } from "../../tools/e2e/api.ts";
import {
  activeRunIdentity,
  apiSucceeded,
  completedJob,
  DEFAULT_WITNESS_DIRECTORY,
  numberValue,
  runIdentityFailures,
  runIdentityFrom,
  runIdentityObservations,
  submitWitnessJob,
  unreportedRunFields,
  type JobApi,
  type RunIdentity,
  type SubmittedJob,
} from "../../tools/e2e/checks/job-survival.ts";
import {
  optionalFlag,
  parseValueFlags,
  requiredFlag,
  runCommand,
  type CommandRunner,
} from "../../tools/e2e/checks/scheduler-proving.ts";

export type { RunIdentity };

/** What the job script itself recorded about its own executions. */
export interface WitnessEvidence {
  /** Distinct hosts the job wrote beats from. Zero when the witness was not read. */
  executionHostCount: number;
  /** Executions beyond the first. Negative when the witness could not be read at all. */
  reruns: number;
}

/** Everything the verdict is computed from. Gathered by the run, asserted by the verdict. */
export interface SurvivalEvidence {
  /**
   * Whether the control plane was ever observed to stop answering. A replacement command that
   * returns without interrupting anything has not replaced the scheduler task, and a test that
   * passed on it would be a test that cannot fail.
   */
  controlPlaneWentAway: boolean;
  /** Run identity after the replacement, or undefined when the job was no longer active. */
  after: RunIdentity | undefined;
  before: RunIdentity;
  /** Exit status of the completed job, or undefined when it never completed. */
  exitStatus: number | undefined;
  jobId: string;
  /** `node_fail_requeue` as configured on the batch server, in seconds. */
  nodeFailRequeueSeconds: number;
  /** Seconds the control plane did not answer, measured across the replacement. */
  outageSeconds: number;
  witness: WitnessEvidence;
}

/** Verdict of the acceptance test: the reasons it failed, or none. */
export interface SurvivalVerdict {
  failures: string[];
  observed: string[];
  passed: boolean;
}

/** Options for one live acceptance run. */
export interface SurvivalOptions {
  /** Arguments for the replacement command. */
  replacementArgs: readonly string[];
  /** The command that replaces the control plane. Whatever an upgrade actually does. */
  replacementExecutable: string;
  jobSleepSeconds: number;
  nodeFailRequeueSeconds: number;
  pollSeconds: number;
  queue: string;
  /** Shared-storage directory the job script writes its witness into. */
  witnessDirectory: string;
  /** Seconds to wait for the job to start, and for the control plane to answer again. */
  timeoutSeconds: number;
}

/** Injected effects, so the verdict logic is provable without a cluster. */
export interface SurvivalDependencies {
  api: JobApi;
  now(): number;
  /** Reads what the job script recorded about its own executions. */
  readWitness(host: string, identifier: string): Promise<WitnessEvidence>;
  replacement: CommandRunner;
  sleep(milliseconds: number): Promise<void>;
}

/**
 * Decides whether the run survived. Pure, so every branch is testable and every failure names
 * a cause rather than a symptom.
 */
export function survivalVerdict(evidence: SurvivalEvidence): SurvivalVerdict {
  const failures: string[] = [
    ...runIdentityFailures(evidence.before, evidence.after),
  ];
  const unreported = unreportedRunFields(evidence.before);
  const observed: string[] = [
    `job=${evidence.jobId}`,
    ...runIdentityObservations(evidence.before, evidence.after),
    ...(unreported.length === 0
      ? []
      : [`not_compared=${unreported.join(",")} the job API did not report them for the active job`]),
    `outage_seconds=${evidence.outageSeconds}`,
    `node_fail_requeue_seconds=${evidence.nodeFailRequeueSeconds}`,
  ];

  if (evidence.witness.reruns < 0) {
    failures.push("the job's witness could not be read, so a second execution cannot be ruled out");
  } else {
    observed.push(
      `witness_reruns=${evidence.witness.reruns}`,
      `witness_execution_hosts=${evidence.witness.executionHostCount}`,
    );
    if (evidence.witness.reruns > 0) {
      failures.push(`the job script ran ${evidence.witness.reruns + 1} times: the run was requeued`);
    }
    if (evidence.witness.executionHostCount > 1) {
      failures.push(
        `the job wrote its beats from ${evidence.witness.executionHostCount} hosts: the run moved execution node`,
      );
    }
  }

  if (!evidence.controlPlaneWentAway) {
    failures.push(
      "the control plane never stopped answering, so the scheduler task was not replaced and this run proves nothing",
    );
  }

  if (evidence.controlPlaneWentAway && evidence.outageSeconds >= evidence.nodeFailRequeueSeconds) {
    failures.push(
      `the control plane was away ${evidence.outageSeconds}s, at or beyond the ${evidence.nodeFailRequeueSeconds}s node-failure requeue window`,
    );
  }

  if (evidence.exitStatus === undefined) {
    failures.push("the job never produced a completed record");
  } else {
    observed.push(`exit_status=${evidence.exitStatus}`);
    if (evidence.exitStatus !== 0) {
      failures.push(`the job finished with exit status ${evidence.exitStatus}`);
    }
  }

  return { failures, observed, passed: failures.length === 0 };
}

/** Runs the acceptance test end to end and returns its verdict. */
export async function runJobSurvivesReplacement(
  options: SurvivalOptions,
  dependencies: SurvivalDependencies,
): Promise<SurvivalVerdict> {
  const identifier = `survive-${randomUUID()}`;
  let job: SubmittedJob;
  try {
    job = await submitWitnessJob(dependencies.api, {
      identifier,
      queue: options.queue,
      sleepSeconds: options.jobSleepSeconds,
      witnessDirectory: options.witnessDirectory,
    });
  } catch (error: unknown) {
    return { failures: [error instanceof Error ? error.message : String(error)], observed: [], passed: false };
  }

  const before = await waitForRunningIdentity(job, options, dependencies);
  if (before === undefined) {
    return {
      failures: [`job ${job.jobId} never reached a running state`],
      observed: [],
      passed: false,
    };
  }

  const replacement = await dependencies.replacement.run(options.replacementExecutable, options.replacementArgs);
  if (replacement.exitCode !== 0) {
    return {
      failures: [`the replacement command failed with exit ${replacement.exitCode}: ${oneLine(replacement.stderr)}`],
      observed: [`job=${job.jobId}`],
      passed: false,
    };
  }

  // The replacement command returns as soon as the platform accepts the deployment, long before
  // the running task stops, so the outage is measured between the control plane going quiet and
  // answering again. Never observing it go quiet is itself a failure.
  const departedAt = await waitForControlPlane(options, dependencies, false);
  const returnedAt =
    departedAt === undefined ? undefined : await waitForControlPlane(options, dependencies, true);
  const outageSeconds =
    departedAt === undefined
      ? 0
      : returnedAt === undefined
        ? options.timeoutSeconds
        : Math.max(0, Math.round((returnedAt - departedAt) / 1_000));
  const after = await activeRunIdentity(dependencies.api, job);
  const completed = await waitForCompletedRun(job, options, dependencies);

  // The completed record is the only one that carries the job's execution hosts, so it is the
  // first choice for reaching the shared home the witness was written to.
  const witnessHost = completed?.executionHost || after?.executionHost || before.executionHost;
  const witness = await dependencies.readWitness(witnessHost, identifier);

  return survivalVerdict({
    after,
    before,
    controlPlaneWentAway: departedAt !== undefined,
    exitStatus: completed?.exitStatus,
    jobId: job.jobId,
    nodeFailRequeueSeconds: options.nodeFailRequeueSeconds,
    outageSeconds,
    witness,
  });
}

/** Polls the active-job API until the batch server reports the job running. */
async function waitForRunningIdentity(
  job: SubmittedJob,
  options: SurvivalOptions,
  dependencies: SurvivalDependencies,
): Promise<RunIdentity | undefined> {
  const deadline = dependencies.now() + options.timeoutSeconds * 1_000;
  while (dependencies.now() <= deadline) {
    const identity = await activeRunIdentity(dependencies.api, job);
    if (identity?.state === "RUNNING") {
      return identity;
    }
    await dependencies.sleep(options.pollSeconds * 1_000);
  }
  return undefined;
}

/**
 * Polls a job listing until it answers, or stops answering, returning the moment it did.
 * Undefined means the deadline passed without the control plane reaching that state.
 */
async function waitForControlPlane(
  options: SurvivalOptions,
  dependencies: SurvivalDependencies,
  wantAnswering: boolean,
): Promise<number | undefined> {
  const deadline = dependencies.now() + options.timeoutSeconds * 1_000;
  while (dependencies.now() <= deadline) {
    let answered = false;
    try {
      // Both the transport and the application field: a 200 carrying success false is a control
      // plane that is not serving, and reading it as alive would understate the outage.
      answered = apiSucceeded(await dependencies.api.request("Scheduler.ListActiveJobs", {}));
    } catch {
      answered = false;
    }
    if (answered === wantAnswering) return dependencies.now();
    await dependencies.sleep(options.pollSeconds * 1_000);
  }
  return undefined;
}

/** Polls the completed-job record for its exit status and the host it finished on. */
async function waitForCompletedRun(
  job: SubmittedJob,
  options: SurvivalOptions,
  dependencies: SurvivalDependencies,
): Promise<{ executionHost: string; exitStatus: number } | undefined> {
  const deadline = dependencies.now() + (options.jobSleepSeconds + options.timeoutSeconds) * 1_000;
  while (dependencies.now() <= deadline) {
    const record = await completedJob(dependencies.api, job);
    const exitStatus = numberValue(record?.["exit_status"]);
    if (record !== undefined && exitStatus !== undefined) {
      return { executionHost: runIdentityFrom(record)?.executionHost ?? "", exitStatus };
    }
    await dependencies.sleep(options.pollSeconds * 1_000);
  }
  return undefined;
}

function oneLine(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

/** Parses the flags of the live runner. */
export function parseSurvivalOptions(argv: readonly string[]): {
  albHost: string;
  clusterName: string;
  options: SurvivalOptions;
  passwordFile: string;
  region: string;
  tokenDirectory: string | undefined;
  username: string;
} {
  const values = parseValueFlags(
    argv,
    new Set([
      "alb-host",
      "cluster-name",
      "aws-region",
      "username",
      "password-file",
      "token-dir",
      "queue",
      "job-sleep-seconds",
      "node-fail-requeue-seconds",
      "poll-seconds",
      "timeout-seconds",
      "witness-dir",
      "replacement",
    ]),
    new Set(),
  );
  const replacement = requiredFlag(values, "replacement").split(" ").filter((part) => part !== "");
  const executable = replacement[0];
  if (executable === undefined) {
    throw new Error("--replacement must name a command");
  }
  return {
    albHost: requiredFlag(values, "alb-host"),
    clusterName: requiredFlag(values, "cluster-name"),
    options: {
      jobSleepSeconds: positiveInteger(optionalFlag(values, "job-sleep-seconds") ?? "1800", "job-sleep-seconds"),
      nodeFailRequeueSeconds: positiveInteger(requiredFlag(values, "node-fail-requeue-seconds"), "node-fail-requeue-seconds"),
      pollSeconds: positiveInteger(optionalFlag(values, "poll-seconds") ?? "10", "poll-seconds"),
      queue: optionalFlag(values, "queue") ?? "normal",
      replacementArgs: replacement.slice(1),
      replacementExecutable: executable,
      timeoutSeconds: positiveInteger(optionalFlag(values, "timeout-seconds") ?? "1800", "timeout-seconds"),
      witnessDirectory: optionalFlag(values, "witness-dir") ?? DEFAULT_WITNESS_DIRECTORY,
    },
    passwordFile: requiredFlag(values, "password-file"),
    region: requiredFlag(values, "aws-region"),
    tokenDirectory: optionalFlag(values, "token-dir"),
    username: requiredFlag(values, "username"),
  };
}

function positiveInteger(value: string, flag: string): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) {
    throw new Error(`--${flag} must be a positive integer`);
  }
  return parsed;
}

/**
 * The witness read command. It runs as the job owner, so `$HOME` in the witness directory is the
 * same directory the job script wrote to, and it refuses when the first run's marker is absent:
 * a missing marker means this is not the job's witness, which is a failed read rather than a run
 * with no reruns. Emits "<reruns> <distinct beat hosts>".
 */
export function witnessReadCommand(input: { directory: string; identifier: string; owner: string }): string {
  const base = `${input.directory}/${input.identifier}`;
  const script = [
    `test -e ${base}.run || exit 3`,
    `printf '%s %s\\n' "$(wc -l < ${base}.reruns 2>/dev/null || echo 0)"`,
    `"$(cut -d' ' -f2 ${base}.beat 2>/dev/null | sort -u | grep -c .)"`,
  ].join(" ");
  return `su ${input.owner} -c ${shellSingleQuote(script)}`;
}

function shellSingleQuote(value: string): string {
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

/**
 * Reads the witness from the shared home through the systems-manager agent, which is the one
 * read path that works whether the control plane is a host or a task. Anything other than the
 * two integers the command prints is an unread witness, never a zero.
 */
export function createWitnessReader(input: {
  awsProfile: string | undefined;
  awsRegion: string;
  owner: string;
  witnessDirectory: string;
  runner?: CommandRunner;
}): (host: string, identifier: string) => Promise<WitnessEvidence> {
  const runner = input.runner ?? { run: runCommand };
  const unread: WitnessEvidence = { executionHostCount: 0, reruns: -1 };
  const profile = input.awsProfile === undefined ? [] : ["--profile", input.awsProfile];
  return async (host, identifier) => {
    if (host === "") return unread;
    const found = await runner.run("aws", [
      "ec2",
      "describe-instances",
      "--region",
      input.awsRegion,
      "--filters",
      `Name=private-ip-address,Values=${host}`,
      "--query",
      "Reservations[].Instances[].InstanceId",
      "--output",
      "text",
      ...profile,
    ]);
    const instanceId = found.stdout.trim().split(/\s+/)[0];
    if (found.exitCode !== 0 || instanceId === undefined || !instanceId.startsWith("i-")) return unread;
    const command = witnessReadCommand({
      directory: input.witnessDirectory,
      identifier,
      owner: input.owner,
    });
    const sent = await runner.run("aws", [
      "ssm",
      "send-command",
      "--region",
      input.awsRegion,
      "--instance-ids",
      instanceId,
      "--document-name",
      "AWS-RunShellScript",
      "--parameters",
      `commands=${JSON.stringify([command])}`,
      "--query",
      "Command.CommandId",
      "--output",
      "text",
      ...profile,
    ]);
    if (sent.exitCode !== 0) return unread;
    const commandId = sent.stdout.trim();
    for (let attempt = 0; attempt < 20; attempt += 1) {
      const invocation = await runner.run("aws", [
        "ssm",
        "get-command-invocation",
        "--region",
        input.awsRegion,
        "--command-id",
        commandId,
        "--instance-id",
        instanceId,
        "--query",
        "StandardOutputContent",
        "--output",
        "text",
        ...profile,
      ]);
      const evidence = parseWitnessOutput(invocation.stdout);
      if (invocation.exitCode === 0 && evidence !== undefined) return evidence;
      await new Promise((resolve) => setTimeout(resolve, 3_000));
    }
    return unread;
  };
}

/** Parses the witness command's output. Undefined for anything that is not its two integers. */
export function parseWitnessOutput(stdout: string): WitnessEvidence | undefined {
  const match = /^\s*(\d+)\s+(\d+)\s*$/.exec(stdout);
  if (match?.[1] === undefined || match[2] === undefined) return undefined;
  return { executionHostCount: Number(match[2]), reruns: Number(match[1]) };
}

/** Entry point for an unattended live run. */
async function main(argv: string[]): Promise<number> {
  const parsed = parseSurvivalOptions(argv);
  const api = new IdeaApiClient({
    albHost: parsed.albHost,
    insecureTls: false,
    passwordFile: parsed.passwordFile,
    tokenDirectory: parsed.tokenDirectory,
    username: parsed.username,
  });
  const verdict = await runJobSurvivesReplacement(parsed.options, {
    api,
    now: () => Date.now(),
    readWitness: createWitnessReader({
      awsProfile: process.env["AWS_PROFILE"],
      awsRegion: parsed.region,
      owner: parsed.username,
      witnessDirectory: parsed.options.witnessDirectory,
    }),
    replacement: { run: runCommand },
    sleep: (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
  });
  for (const line of verdict.observed) process.stdout.write(`OBSERVED ${line}\n`);
  for (const line of verdict.failures) process.stdout.write(`FAILED ${line}\n`);
  process.stdout.write(verdict.passed ? "PASS the run survived the replacement\n" : "FAIL\n");
  return verdict.passed ? 0 : 1;
}

if (process.argv[1] !== undefined && import.meta.url === `file://${process.argv[1]}`) {
  main(process.argv.slice(2)).then(
    (code) => process.exit(code),
    (error: unknown) => {
      process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
      process.exit(2);
    },
  );
}
