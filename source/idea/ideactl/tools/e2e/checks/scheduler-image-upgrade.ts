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
import { measureUpgradeAvailability, type AvailabilitySample } from "./upgrade-availability.ts";
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
  description: "Measure portal, scheduler and gateway availability during the upgrade command and prove a running job was not requeued.",
  requiredFlags: (options) => [
    ...(options.albHost === undefined ? ["alb-host"] : []),
    ...(options.username === undefined ? ["username"] : []),
    ...(options.passwordFile === undefined ? ["password-file"] : []),
    ...(options.cluster === undefined ? ["cluster"] : []),
    ...(options.region === undefined ? ["region"] : []),
    ...(options.schedulerService === undefined ? ["scheduler-service"] : []),
    ...(options.gatewayHost === undefined ? ["gateway-host"] : []),
    ...(options.upgradeCommand === undefined ? ["upgrade-command"] : []),
  ],
  async run(context) {
    context.output("ACTION submit a witnessed scheduler job");
    const identifier = `scheduler-image-upgrade-${randomUUID()}`;
    const sleepSeconds = context.options.jobSleepSeconds ?? DEFAULT_JOB_SLEEP_SECONDS;
    const submittedAt = Date.now();
    const job = await submitWitnessJob(context.api, {
      identifier,
      queue: QUEUE,
      sleepSeconds,
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
    const { result: upgrade, availability } = await measureUpgradeAvailability(context, () => context.processes.run("sh", ["-c", command]));
    const availabilityObservations = availability.flatMap((row) => [
      `availability endpoint=${row.endpoint} samples=${row.samples} failures=${row.failures} slow=${row.slow} longest_gap_ms=${row.longestGapMs}`,
      ...row.failureDetails.map((detail) => `availability failure endpoint=${row.endpoint} at=${detail}`),
    ]);
    const continuous = upgradeIsContinuous(availability);
    const upgradeSeconds = Math.round((context.now() - startedAt) / 1_000);
    // The whole transcript, on disk and named: a deploy tool's reason for stopping is rarely in
    // the last two kilobytes of stderr.
    const transcript = join(mkdtempSync(join(tmpdir(), "scheduler-image-upgrade-")), "upgrade.log");
    writeFileSync(transcript, `${upgrade.stdout}\n--- stderr ---\n${upgrade.stderr}`);
    context.output(`OBSERVED upgrade transcript: ${transcript}`);
    if (upgrade.exitCode !== 0) {
      return failed(...availabilityObservations, `job=${job.jobId}`, `upgrade command exited ${upgrade.exitCode} after ${upgradeSeconds}s`, `transcript=${transcript}`, upgrade.stderr.trim().slice(-2_000));
    }
    const recovery = await waitForServiceRunning(context, requiredOption(context.options, "schedulerService"));
    if (!recovery.passed) {
      return { ...recovery, observed: [...availabilityObservations, `job=${job.jobId}`, `upgrade command exited 0 after ${upgradeSeconds}s`, ...recovery.observed] };
    }
    const after = await activeRunIdentity(context.api, job);
    const observed = [
      ...availabilityObservations,
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
    // The job cannot finish before its own sleep; wait for that plus the usual completion allowance.
    const remainingSleep = Math.max(0, sleepSeconds - Math.floor((Date.now() - submittedAt) / 1000));
    const finish = await waitForWitnessedFinish(context, job, remainingSleep + 600);
    return finish.passed && continuous ? passed(...observed, ...finish.observed) : failed(...observed, ...finish.observed);
  },
};


export function upgradeIsContinuous(availability: AvailabilitySample[]): boolean {
  // The gateway budget is a design target, not a fit to the last run: a new connection may wait at most 15 s.
  return availability.filter(row => row.endpoint !== "scheduler").every(row => row.endpoint === "gateway" ? row.longestGapMs <= 15_000 : row.failures === 0);
}
