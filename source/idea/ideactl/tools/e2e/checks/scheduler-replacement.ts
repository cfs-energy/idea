import { randomUUID } from "node:crypto";

import {
  apiSucceeded,
  failed,
  passed,
  requiredOption,
  waitForServiceRunning,
  waitUntil,
} from "./shared.ts";
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
import type { CheckContext, ProofCheck } from "./types.ts";

/** The queue this check submits to. The same queue the other job checks use. */
const QUEUE = "normal";

/**
 * Long enough that the job is still running after the replacement completes. A job that
 * finishes during the replacement leaves nothing to compare and fails for the wrong reason.
 */
const DEFAULT_JOB_SLEEP_SECONDS = 1_800;

/**
 * Proves a running job survives replacement of the scheduler task.
 *
 * Not that the job eventually completes: a requeued job also completes, with the expected
 * exit status, from the same submission, having discarded the work already done. The job
 * script therefore witnesses its own second execution and exits WITNESS_RERUN_EXIT_STATUS,
 * so a requeue cannot reach the expected status. The run's start time is compared as well
 * wherever the job API reports it, and the result names it as unreported when it does not.
 */
export const schedulerReplacementCheck: ProofCheck = {
  name: "scheduler-replacement",
  description: "Replace the scheduler task while a witnessed job is running, and prove the run was not requeued.",
  requiredFlags: (options) => [
    ...(options.albHost === undefined ? ["alb-host"] : []),
    ...(options.username === undefined ? ["username"] : []),
    ...(options.passwordFile === undefined ? ["password-file"] : []),
    ...(options.cluster === undefined ? ["cluster"] : []),
    ...(options.region === undefined ? ["region"] : []),
    ...(options.schedulerService === undefined ? ["scheduler-service"] : []),
    ...(options.schedulerTask === undefined ? ["scheduler-task"] : []),
  ],
  async run(context) {
    context.output("ACTION submit a witnessed scheduler job");
    const identifier = `scheduler-replacement-${randomUUID()}`;
    const job = await submitWitnessJob(context.api, {
      identifier,
      queue: QUEUE,
      sleepSeconds: context.options.jobSleepSeconds ?? DEFAULT_JOB_SLEEP_SECONDS,
      witnessDirectory: DEFAULT_WITNESS_DIRECTORY,
    });
    const before = await waitForRunningIdentity(context, job);
    if (before === undefined) {
      return failed(`job=${job.jobId}`, "the job never reached a running state");
    }
    const unreported = unreportedRunFields(before);

    context.output(`ACTION stop scheduler task ${requiredOption(context.options, "schedulerTask")}`);
    await context.cloud.stopTask(requiredOption(context.options, "cluster"), requiredOption(context.options, "schedulerTask"));
    const recovery = await waitForServiceRunning(context, requiredOption(context.options, "schedulerService"));
    if (!recovery.passed) {
      return { ...recovery, observed: [`job=${job.jobId}`, ...recovery.observed] };
    }

    const after = await activeRunIdentity(context.api, job);
    const observed = [
      `job=${job.jobId}`,
      ...recovery.observed,
      ...runIdentityObservations(before, after),
      ...(unreported.length === 0
        ? []
        : [`not_compared=${unreported.join(",")} the job API did not report them for the active job`]),
    ];
    const identityFailures = runIdentityFailures(before, after);
    if (identityFailures.length > 0) {
      return failed(...observed, ...identityFailures);
    }

    const queue = await context.api.request("Scheduler.ListActiveJobs", {});
    if (!apiSucceeded(queue)) {
      return failed(...observed, `queue response=${JSON.stringify(queue.body)}`);
    }
    observed.push("queue responded after replacement");

    const finish = await waitForWitnessedFinish(context, job);
    return finish.passed ? passed(...observed, ...finish.observed) : failed(...observed, ...finish.observed);
  },
};

/** Polls the active job until the batch server reports it running. */
async function waitForRunningIdentity(context: CheckContext, job: SubmittedJob): Promise<RunIdentity | undefined> {
  let identity: RunIdentity | undefined;
  const running = await waitUntil(context, context.options.readyTimeoutSeconds ?? 1_800, `job ${job.jobId} to start`, async () => {
    identity = await activeRunIdentity(context.api, job);
    return identity?.state === "RUNNING";
  });
  return running ? identity : undefined;
}

/**
 * Waits for the completed record and reads its exit status. The witness script's own
 * rerun status is reported as a requeue rather than as an unexpected exit code, because
 * that is what it means.
 */
async function waitForWitnessedFinish(
  context: CheckContext,
  job: SubmittedJob,
): Promise<{ observed: string[]; passed: boolean }> {
  const expected = context.options.expectedExitStatus ?? 0;
  let exitStatus: number | undefined;
  const finished = await waitUntil(context, context.options.readyTimeoutSeconds ?? 1_800, `completion of job ${job.jobUid}`, async () => {
    exitStatus = numberValue((await completedJob(context.api, job))?.["exit_status"]);
    return exitStatus !== undefined;
  });
  if (!finished || exitStatus === undefined) {
    return { observed: ["the job produced no completed record"], passed: false };
  }
  const observed = [`exit_status=${exitStatus}`];
  if (exitStatus === WITNESS_RERUN_EXIT_STATUS && expected !== WITNESS_RERUN_EXIT_STATUS) {
    return {
      observed: [...observed, "the job script ran a second time: the run was requeued, not carried across the replacement"],
      passed: false,
    };
  }
  return exitStatus === expected
    ? { observed, passed: true }
    : { observed: [...observed, `expected exit status ${expected}`], passed: false };
}
