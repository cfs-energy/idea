/**
 * Both replacement checks, sabotaged in both directions.
 *
 * A requeued job reaches "completed with the expected exit status" too: same submission, same
 * script, from the beginning. So each check is exercised twice, once on a genuine survival and
 * once on a requeue whose start time moved, whose second execution exited 17, and whose
 * execution node did not change, which no host or instance comparison would catch.
 */

import assert from "node:assert/strict";
import test from "node:test";

import type { ApiCallResult, JsonValue } from "../../tools/e2e/api.ts";
import { WITNESS_RERUN_EXIT_STATUS } from "../../tools/e2e/checks/job-survival.ts";
import { schedulerReplacementCheck } from "../../tools/e2e/checks/scheduler-replacement.ts";
import {
  runSchedulerVmReplacementCheck,
  type SchedulerVmReplacementDependencies,
} from "../../tools/e2e/checks/scheduler-vm-replacement.ts";
import type { CheckContext, ProofMatrixOptions } from "../../tools/e2e/checks/types.ts";

/** What the batch server reported for the job before and after the replacement. */
interface Reported {
  executionHost: string;
  exitStatus: number;
  instanceId: string;
  startTimeAfter: string;
  startTimeBefore: string;
}

/** A survival: the same run, on the same node, finishing cleanly. */
const survived: Reported = {
  executionHost: "192.0.2.169",
  exitStatus: 0,
  instanceId: "i-0123456789abcdef2",
  startTimeAfter: "2026-09-11T03:12:37Z",
  startTimeBefore: "2026-09-11T03:12:37Z",
};

/** A requeue: new start time, same node, exit 17. */
const requeued: Reported = {
  executionHost: "192.0.2.53",
  exitStatus: WITNESS_RERUN_EXIT_STATUS,
  instanceId: "i-0123456789abcdef1",
  startTimeAfter: "2026-09-11T04:02:09Z",
  startTimeBefore: "2026-09-11T03:54:14Z",
};

/** The same requeue on a cluster whose job API reports no start time for an active job. */
const requeuedWithNoStartTime: Reported = { ...requeued, startTimeAfter: "", startTimeBefore: "" };

/** Answers the four namespaces both checks use, switching identity once the task was stopped. */
function jobApi(reported: Reported, replaced: () => boolean): {
  scripts: string[];
  request(namespace: string, payload: JsonValue): Promise<ApiCallResult>;
} {
  const scripts: string[] = [];
  return {
    scripts,
    async request(namespace, payload): Promise<ApiCallResult> {
      if (namespace === "Scheduler.SubmitJob") {
        const script = (payload as { job_script?: string }).job_script ?? "";
        scripts.push(Buffer.from(script, "base64").toString("utf8"));
        return { body: { payload: { job: { job_id: "3", job_uid: "uid-3" } }, success: true }, status: 200 };
      }
      if (namespace === "Scheduler.ListActiveJobs") {
        return { body: { payload: { jobs: [] }, success: true }, status: 200 };
      }
      if (namespace === "Scheduler.GetCompletedJob") {
        return {
          body: { payload: { job: { exit_status: reported.exitStatus } }, success: true },
          status: 200,
        };
      }
      if (namespace === "Scheduler.GetActiveJob") {
        const startTime = replaced() ? reported.startTimeAfter : reported.startTimeBefore;
        return {
          body: {
            payload: {
              job: {
                execution_hosts: [{ host: reported.executionHost, instance_id: reported.instanceId }],
                start_time: startTime,
                state: "running",
              },
            },
            success: true,
          },
          status: 200,
        };
      }
      throw new Error(`unexpected API request: ${namespace}`);
    },
  };
}

const options: ProofMatrixOptions = {
  albHost: "control.example.invalid",
  checks: ["scheduler-replacement"],
  cluster: "sample-containers",
  insecureTls: false,
  passwordFile: "/secure/password",
  pollSeconds: 1,
  readyTimeoutSeconds: 60,
  region: "us-east-2",
  schedulerService: "sample-scheduler",
  schedulerTask: "task-1",
  username: "tester",
};

/** A check context whose clock advances only when the check sleeps. */
function context(reported: Reported): { context: CheckContext; output: string[]; scripts: string[] } {
  let clock = 1_000_000;
  let replaced = false;
  const output: string[] = [];
  const api = jobApi(reported, () => replaced);
  return {
    output,
    scripts: api.scripts,
    context: {
      api,
      cloud: {
        async healthyTargetCount() {
          return 1;
        },
        async serviceRunningCount() {
          return { desired: 1, running: 1 };
        },
        async stopTask() {
          replaced = true;
        },
      },
      gateway: {
        async connect() {
          throw new Error("the scheduler replacement check does not use the gateway");
        },
      },
      now: () => clock,
      options,
      output: (line) => output.push(line),
      processes: {
        async run() {
          throw new Error("the scheduler replacement check runs no processes");
        },
      },
      async sleep(milliseconds) {
        clock += milliseconds;
      },
    },
  };
}

test("scheduler-replacement submits the witnessing script, so a requeue cannot exit cleanly", async () => {
  const harness = context(survived);
  await schedulerReplacementCheck.run(harness.context);
  assert.equal(harness.scripts.length, 1);
  const script = harness.scripts[0] ?? "";
  assert.match(script, new RegExp(`exit ${WITNESS_RERUN_EXIT_STATUS}`));
  assert.match(script, /if \[ -e \$HOME\/\.idea-job-witness\//);
});

test("scheduler-replacement passes on a genuine survival", async () => {
  const harness = context(survived);
  const result = await schedulerReplacementCheck.run(harness.context);
  assert.equal(result.passed, true, result.observed.join("; "));
  assert.ok(result.observed.includes("start_time_after=2026-09-11T03:12:37Z"));
  assert.ok(result.observed.includes("exit_status=0"));
});

test("scheduler-replacement fails on a requeue that kept its execution node", async () => {
  const harness = context(requeued);
  const result = await schedulerReplacementCheck.run(harness.context);
  assert.equal(result.passed, false);
  const observed = result.observed.join("\n");
  assert.match(observed, /start time moved from 2026-09-11T03:54:14Z to 2026-09-11T04:02:09Z/);
  assert.doesNotMatch(observed, /execution node was replaced|moved execution host/);
});

test("scheduler-replacement fails on a requeue with no start time reported, on the witness alone", async () => {
  const harness = context(requeuedWithNoStartTime);
  const result = await schedulerReplacementCheck.run(harness.context);
  assert.equal(result.passed, false);
  const observed = result.observed.join("\n");
  assert.match(observed, /the job script ran a second time: the run was requeued/);
  assert.match(observed, /not_compared=start_time/);
});

/** The virtual-machine check reaches its scheduler over SSH, and replaces the whole host. */
function vmDependencies(reported: Reported): {
  dependencies: SchedulerVmReplacementDependencies;
  scripts: string[];
} {
  let clock = 1_000_000;
  let replaced = false;
  let identityReads = 0;
  const api = jobApi(reported, () => replaced);
  return {
    scripts: api.scripts,
    dependencies: {
      api,
      now: () => clock,
      remote: {
        async run(_host, command) {
          if (command === "cat /etc/machine-id") {
            identityReads += 1;
            const identity = identityReads === 1 || !replaced ? "a".repeat(32) : "b".repeat(32);
            return { exitCode: 0, stderr: "", stdout: `${identity}\n` };
          }
          if (command === '/opt/pbs/bin/qmgr -c "list queue"') {
            return { exitCode: 0, stderr: "", stdout: "Queue normal\nQueue test\n" };
          }
          return { exitCode: 0, stderr: "", stdout: "" };
        },
      },
      replacement: {
        async run() {
          replaced = true;
          clock += 1_000;
          return { exitCode: 0, stderr: "", stdout: "" };
        },
      },
      async sleep(milliseconds) {
        clock += milliseconds;
      },
    },
  };
}

const vmOptions = {
  expectedExitStatus: 0,
  jobSleepSeconds: 1_800,
  pollSeconds: 1,
  replacementArgs: ["deploy", "--upgrade", "scheduler"],
  replacementExecutable: "ideactl",
  schedulerHost: "scheduler.sample-cluster.us-east-2.local",
  timeoutSeconds: 60,
};

test("scheduler-vm-replacement passes on a genuine survival and submits the witnessing script", async () => {
  const harness = vmDependencies(survived);
  const result = await runSchedulerVmReplacementCheck(vmOptions, harness.dependencies);
  assert.equal(result.passed, true, result.observed.join("; "));
  assert.match(harness.scripts[0] ?? "", new RegExp(`exit ${WITNESS_RERUN_EXIT_STATUS}`));
  assert.ok(result.observed.includes("queues_before=normal,test"));
});

test("scheduler-vm-replacement fails on a requeue that kept its execution node", async () => {
  const harness = vmDependencies(requeued);
  const result = await runSchedulerVmReplacementCheck(vmOptions, harness.dependencies);
  assert.equal(result.passed, false);
  const observed = result.observed.join("\n");
  assert.match(observed, /start time moved from 2026-09-11T03:54:14Z to 2026-09-11T04:02:09Z/);
  assert.doesNotMatch(observed, /execution node was replaced/);
});

test("scheduler-vm-replacement fails on a requeue with no start time reported", async () => {
  const harness = vmDependencies(requeuedWithNoStartTime);
  const result = await runSchedulerVmReplacementCheck(vmOptions, harness.dependencies);
  assert.equal(result.passed, false);
  const observed = result.observed.join("\n");
  assert.match(observed, /the job script ran a second time: the run was requeued/);
  assert.match(observed, /not_compared=start_time/);
});
