import { failed, submitJob, waitForJobCompletion } from "./shared.ts";
import type { ProofCheck } from "./types.ts";

/** Proves a concurrent PBS burst reaches the requested terminal exit status. */
export const jobBurstCheck: ProofCheck = {
  name: "job-burst",
  description: "Submit a burst of short PBS jobs and verify every exit status.",
  requiredFlags: (options) => [
    ...(options.albHost === undefined ? ["alb-host"] : []),
    ...(options.username === undefined ? ["username"] : []),
    ...(options.passwordFile === undefined ? ["password-file"] : []),
  ],
  async run(context) {
    const count = context.options.jobCount ?? 12;
    const startedAt = context.now();
    context.output(`ACTION submit ${count} short scheduler jobs`);
    const jobUids = await Promise.all(Array.from({ length: count }, () => submitJob(context, "burst")));
    const results = await Promise.all(
      jobUids.map(async (jobUid) => ({ completedAt: context.now(), jobUid, result: await waitForJobCompletion(context, jobUid) })),
    );
    const failedResults = results.filter(({ result }) => !result.passed);
    const slowestMs = Math.max(...results.map(({ completedAt }) => completedAt - startedAt));
    const observed = [
      `submitted=${jobUids.length}`,
      `slowest=${slowestMs}ms`,
      ...results.flatMap(({ result }) => result.observed),
    ];
    return failedResults.length === 0
      ? { observed, passed: true }
      : failed(`failed=${failedResults.length}/${count}`, ...observed);
  },
};
