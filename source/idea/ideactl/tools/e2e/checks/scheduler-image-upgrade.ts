import { randomUUID } from "node:crypto";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { apiSucceeded, failed, passed, requiredOption, waitForServiceRunning } from "./shared.ts";
import {
  activeRunIdentity,
  DEFAULT_WITNESS_DIRECTORY,
  runIdentityFailures,
  runIdentityObservations,
  submitWitnessJob,
  unreportedRunFields,
} from "./job-survival.ts";
import { waitForRunningIdentity, waitForWitnessedFinish } from "./scheduler-replacement.ts";
import type { ProofCheck } from "./types.ts";

/** The queue this check submits to. The same queue the other job checks use. */
const QUEUE = "normal";

/** Long enough that the job is still running after the upgrade completes. */
const DEFAULT_JOB_SLEEP_SECONDS = 1_800;

/**
 * Proves a running job survives the operator's own upgrade path, not a hand-stopped task.
 *
 * The scheduler-replacement check stops one task and watches the service recover. This check
 * runs the command an operator runs to upgrade a cluster (a new image tag rolled through
 * `upgrade-cluster`, for instance) while a witnessed job is running, then applies the same
 * survival proof: the witness script exits its rerun status on a requeue, the run identity is
 * compared across the upgrade, and the service must report every desired task running.
 */
export const schedulerImageUpgradeCheck: ProofCheck = {
  name: "scheduler-image-upgrade",
  description: "Run the operator's upgrade command while a witnessed job is running, and prove the run was not requeued.",
  requiredFlags: (options) => [
    ...(options.albHost === undefined ? ["alb-host"] : []),
    ...(options.username === undefined ? ["username"] : []),
    ...(options.passwordFile === undefined ? ["password-file"] : []),
    ...(options.cluster === undefined ? ["cluster"] : []),
    ...(options.region === undefined ? ["region"] : []),
    ...(options.schedulerService === undefined ? ["scheduler-service"] : []),
    ...(options.upgradeCommand === undefined ? ["upgrade-command"] : []),
  ],
  async run(context) {
    context.output("ACTION submit a witnessed scheduler job");
    const identifier = `scheduler-image-upgrade-${randomUUID()}`;
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
    const command = requiredOption(context.options, "upgradeCommand");
    context.output(`ACTION run the upgrade command: ${command}`);
    const startedAt = context.now();
    const upgrade = await context.processes.run("sh", ["-c", command]);
    const upgradeSeconds = Math.round((context.now() - startedAt) / 1_000);
    // The whole transcript, on disk and named: a deploy tool's reason for stopping is rarely in
    // the last two kilobytes of stderr.
    const transcript = join(mkdtempSync(join(tmpdir(), "scheduler-image-upgrade-")), "upgrade.log");
    writeFileSync(transcript, `${upgrade.stdout}\n--- stderr ---\n${upgrade.stderr}`);
    context.output(`OBSERVED upgrade transcript: ${transcript}`);
    if (upgrade.exitCode !== 0) {
      return failed(`job=${job.jobId}`, `upgrade command exited ${upgrade.exitCode} after ${upgradeSeconds}s`, `transcript=${transcript}`, upgrade.stderr.trim().slice(-2_000));
    }
    const recovery = await waitForServiceRunning(context, requiredOption(context.options, "schedulerService"));
    if (!recovery.passed) {
      return { ...recovery, observed: [`job=${job.jobId}`, `upgrade command exited 0 after ${upgradeSeconds}s`, ...recovery.observed] };
    }
    const after = await activeRunIdentity(context.api, job);
    const observed = [
      `job=${job.jobId}`,
      `upgrade command exited 0 after ${upgradeSeconds}s`,
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
    observed.push("queue responded after the upgrade");
    const finish = await waitForWitnessedFinish(context, job);
    return finish.passed ? passed(...observed, ...finish.observed) : failed(...observed, ...finish.observed);
  },
};
