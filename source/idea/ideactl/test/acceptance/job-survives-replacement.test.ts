import assert from "node:assert/strict";
import test from "node:test";

import type { ApiCallResult, JsonValue } from "../../tools/e2e/api.ts";
import { witnessJobScript, WITNESS_RERUN_EXIT_STATUS } from "../../tools/e2e/checks/job-survival.ts";
import {
  createWitnessReader,
  parseSurvivalOptions,
  parseWitnessOutput,
  runJobSurvivesReplacement,
  survivalVerdict,
  witnessReadCommand,
  type RunIdentity,
  type SurvivalDependencies,
  type SurvivalEvidence,
} from "./job-survives-replacement.ts";

const runningOn: RunIdentity = {
  executionHost: "192.0.2.44",
  instanceId: "i-0123456789abcdef0",
  startTime: "2026-09-10T12:00:00Z",
  state: "RUNNING",
};

/** Evidence of a clean survival, which every case below perturbs in exactly one way. */
function survived(overrides: Partial<SurvivalEvidence> = {}): SurvivalEvidence {
  return {
    after: { ...runningOn },
    before: { ...runningOn },
    controlPlaneWentAway: true,
    exitStatus: 0,
    jobId: "3",
    nodeFailRequeueSeconds: 600,
    outageSeconds: 240,
    witness: { executionHostCount: 1, reruns: 0 },
    ...overrides,
  };
}

test("a run whose identity is unchanged across the replacement passes", () => {
  const verdict = survivalVerdict(survived());
  assert.equal(verdict.passed, true, verdict.failures.join("; "));
  assert.deepEqual(verdict.failures, []);
  assert.ok(verdict.observed.includes("outage_seconds=240"));
});

test("a requeued run fails even though the job completed cleanly", () => {
  const verdict = survivalVerdict(
    survived({ after: { ...runningOn, startTime: "2026-09-10T12:06:00Z" } }),
  );
  assert.equal(verdict.passed, false);
  assert.match(verdict.failures.join("\n"), /restarted the run: start time moved/);
});

test("a second execution of the job script fails the run", () => {
  const verdict = survivalVerdict(survived({ witness: { executionHostCount: 1, reruns: 1 } }));
  assert.equal(verdict.passed, false);
  assert.match(verdict.failures.join("\n"), /ran 2 times: the run was requeued/);
});

test("beats from more than one host fail the run", () => {
  const verdict = survivalVerdict(survived({ witness: { executionHostCount: 2, reruns: 0 } }));
  assert.equal(verdict.passed, false);
  assert.match(verdict.failures.join("\n"), /beats from 2 hosts: the run moved execution node/);
});

test("a replaced execution node fails the run", () => {
  const verdict = survivalVerdict(
    survived({ after: { ...runningOn, instanceId: "i-00000000000000001" } }),
  );
  assert.equal(verdict.passed, false);
  assert.match(verdict.failures.join("\n"), /execution node was replaced/);
});

test("a job that is no longer active fails the run", () => {
  const verdict = survivalVerdict(survived({ after: undefined }));
  assert.equal(verdict.passed, false);
  assert.match(verdict.failures.join("\n"), /not active after the replacement/);
});

test("an outage at or beyond the requeue window fails even when the job survived", () => {
  const verdict = survivalVerdict(survived({ nodeFailRequeueSeconds: 310, outageSeconds: 310 }));
  assert.equal(verdict.passed, false);
  assert.match(verdict.failures.join("\n"), /at or beyond the 310s node-failure requeue window/);
});

test("an unreadable witness is a failure, not a silent pass", () => {
  const verdict = survivalVerdict(survived({ witness: { executionHostCount: 0, reruns: -1 } }));
  assert.equal(verdict.passed, false);
  assert.match(verdict.failures.join("\n"), /witness could not be read/);
});

test("a non-zero exit status fails the run", () => {
  const verdict = survivalVerdict(survived({ exitStatus: 17 }));
  assert.equal(verdict.passed, false);
  assert.match(verdict.failures.join("\n"), /exit status 17/);
});

test("a job that never completes fails the run", () => {
  const verdict = survivalVerdict(survived({ exitStatus: undefined }));
  assert.equal(verdict.passed, false);
  assert.match(verdict.failures.join("\n"), /never produced a completed record/);
});

test("identity fields the job API did not report are named, not compared as equal", () => {
  const unreported: RunIdentity = { executionHost: "", instanceId: "", startTime: "", state: "RUNNING" };
  const verdict = survivalVerdict(survived({ after: { ...unreported }, before: { ...unreported } }));
  assert.equal(verdict.passed, true, verdict.failures.join("; "));
  assert.ok(verdict.observed.includes("start_time_before=unreported"));
  assert.ok(verdict.observed.includes("instance_before=unreported"));
});

test("the witness script exits non-zero on a second execution", () => {
  const script = witnessJobScript({
    identifier: "survive-1",
    queue: "normal",
    sleepSeconds: 60,
    witnessDirectory: "/data/home/tester/.idea-job-witness",
  });
  assert.match(script, /#PBS -q normal/);
  assert.match(script, /if \[ -e \/data\/home\/tester\/\.idea-job-witness\/survive-1\.run \]; then/);
  assert.match(script, new RegExp(`exit ${WITNESS_RERUN_EXIT_STATUS}`));
  // The marker is written before the wait, so an interrupted first run still witnesses itself.
  assert.ok(
    script.indexOf("survive-1.run") < script.indexOf("sleep 10"),
    "the marker must be written before the job waits",
  );
});

/**
 * A requeue that reused the same execution node: the host and instance comparisons both hold,
 * so only the start time and the witness catch it.
 */
test("the requeue observed live fails the verdict, and not because the node moved", () => {
  const before: RunIdentity = {
    executionHost: "192.0.2.53",
    instanceId: "i-0123456789abcdef1",
    startTime: "2026-09-11T03:54:14Z",
    state: "RUNNING",
  };
  const verdict = survivalVerdict({
    after: { ...before, startTime: "2026-09-11T04:02:09Z" },
    before,
    controlPlaneWentAway: true,
    exitStatus: WITNESS_RERUN_EXIT_STATUS,
    jobId: "3",
    nodeFailRequeueSeconds: 310,
    outageSeconds: 183,
    witness: { executionHostCount: 1, reruns: 1 },
  });
  assert.equal(verdict.passed, false);
  const failures = verdict.failures.join("\n");
  assert.match(failures, /start time moved from 2026-09-11T03:54:14Z to 2026-09-11T04:02:09Z/);
  assert.match(failures, /ran 2 times: the run was requeued/);
  assert.match(failures, /exit status 17/);
  assert.doesNotMatch(failures, /execution node was replaced|moved execution host/);
});

/** A survival: the same run, on the same node, finishing cleanly. */
test("the survival observed live passes the verdict", () => {
  const before: RunIdentity = {
    executionHost: "192.0.2.169",
    instanceId: "i-0123456789abcdef2",
    startTime: "2026-09-11T03:12:37Z",
    state: "RUNNING",
  };
  const verdict = survivalVerdict({
    after: { ...before },
    before,
    controlPlaneWentAway: true,
    exitStatus: 0,
    jobId: "2",
    nodeFailRequeueSeconds: 310,
    outageSeconds: 183,
    witness: { executionHostCount: 1, reruns: 0 },
  });
  assert.equal(verdict.passed, true, verdict.failures.join("; "));
});

test("the witness read runs as the job owner and refuses when the marker is absent", () => {
  const command = witnessReadCommand({
    directory: "$HOME/.idea-job-witness",
    identifier: "survive-1",
    owner: "tester",
  });
  assert.match(command, /^su tester -c '/);
  // $HOME must reach the remote shell unexpanded: the reader runs as root and the witness
  // is in the job owner's home.
  assert.match(command, /\$HOME\/\.idea-job-witness\/survive-1\.run \|\| exit 3/);
  assert.match(command, /wc -l < \$HOME\/\.idea-job-witness\/survive-1\.reruns/);
});

test("a witness read that returns anything but its two counts is unread, never zero", () => {
  assert.deepEqual(parseWitnessOutput("1 1\n"), { executionHostCount: 1, reruns: 1 });
  assert.deepEqual(parseWitnessOutput("  0   1  "), { executionHostCount: 1, reruns: 0 });
  // A command that failed prints nothing. Reading that as "no reruns" is the silent pass
  // this check exists to prevent.
  assert.equal(parseWitnessOutput(""), undefined);
  assert.equal(parseWitnessOutput("0"), undefined);
  assert.equal(parseWitnessOutput("An error occurred (AccessDenied)"), undefined);
});

test("an unresolvable execution host is an unread witness", async () => {
  const reader = createWitnessReader({
    awsProfile: undefined,
    awsRegion: "us-east-2",
    owner: "tester",
    witnessDirectory: "$HOME/.idea-job-witness",
    runner: {
      async run() {
        throw new Error("no command should be run without a host");
      },
    },
  });
  assert.deepEqual(await reader("", "survive-1"), { executionHostCount: 0, reruns: -1 });
});

/** Builds an API double whose active-job answer changes once the replacement has been invoked. */
function apiDouble(input: {
  afterIdentity: RunIdentity | undefined;
  beforeIdentity: RunIdentity;
  consumeQuiet: () => boolean;
  exitStatus: number;
  replaced: () => boolean;
}): { calls: string[]; request: (namespace: string, payload: JsonValue) => Promise<ApiCallResult> } {
  const calls: string[] = [];
  const jobBody = (identity: RunIdentity, extra: Record<string, JsonValue> = {}): ApiCallResult => ({
    body: {
      payload: {
        job: {
          execution_hosts: [{ host: identity.executionHost, instance_id: identity.instanceId }],
          start_time: identity.startTime,
          state: identity.state,
          ...extra,
        },
      },
      success: true,
    },
    status: 200,
  });
  return {
    calls,
    async request(namespace: string, _payload: JsonValue): Promise<ApiCallResult> {
      calls.push(namespace);
      if (namespace === "Scheduler.SubmitJob") {
        return { body: { payload: { job: { job_id: "3", job_uid: "uid-3" } }, success: true }, status: 200 };
      }
      if (namespace === "Scheduler.ListActiveJobs") {
        // Quiet for the first two listings after the replacement, the way a stopped task is.
        if (input.replaced() && input.consumeQuiet()) {
          return { body: { success: false }, status: 503 };
        }
        return { body: { payload: { jobs: [] }, success: true }, status: 200 };
      }
      if (namespace === "Scheduler.GetCompletedJob") {
        return jobBody(input.afterIdentity ?? input.beforeIdentity, { exit_status: input.exitStatus });
      }
      const identity = input.replaced() ? input.afterIdentity : input.beforeIdentity;
      if (identity === undefined) {
        return { body: { success: false }, status: 404 };
      }
      return jobBody(identity);
    },
  };
}

/** Wires the double into the live runner with time advanced by the sleeps it asks for. */
function dependencies(input: {
  afterIdentity: RunIdentity | undefined;
  beforeIdentity?: RunIdentity;
  exitStatus?: number;
  quietListings?: number;
  replacementExit?: number;
  witnessReruns?: number;
}): { deps: SurvivalDependencies; replacementCalls: string[][]; witnessHosts: string[] } {
  let replaced = false;
  let quietListings = input.quietListings ?? 2;
  let clock = 1_000_000;
  const replacementCalls: string[][] = [];
  const witnessHosts: string[] = [];
  const api = apiDouble({
    afterIdentity: input.afterIdentity,
    beforeIdentity: input.beforeIdentity ?? runningOn,
    consumeQuiet: () => {
      if (quietListings <= 0) return false;
      quietListings -= 1;
      return true;
    },
    exitStatus: input.exitStatus ?? 0,
    replaced: () => replaced,
  });
  return {
    replacementCalls,
    witnessHosts,
    deps: {
      api,
      now: () => clock,
      async readWitness(host) {
        witnessHosts.push(host);
        return { executionHostCount: 1, reruns: input.witnessReruns ?? 0 };
      },
      replacement: {
        async run(executable, args) {
          replacementCalls.push([executable, ...args]);
          replaced = true;
          clock += 120_000;
          return { exitCode: input.replacementExit ?? 0, stderr: "", stdout: "" };
        },
      },
      async sleep(milliseconds) {
        clock += milliseconds;
      },
    },
  };
}

const liveOptions = {
  jobSleepSeconds: 1_800,
  nodeFailRequeueSeconds: 600,
  pollSeconds: 10,
  queue: "normal",
  replacementArgs: ["update-service", "--force-new-deployment"],
  replacementExecutable: "aws",
  timeoutSeconds: 900,
  witnessDirectory: "/data/home/tester/.idea-job-witness",
};

test("a replacement that never interrupted the control plane cannot pass", () => {
  const verdict = survivalVerdict(survived({ controlPlaneWentAway: false, outageSeconds: 0 }));
  assert.equal(verdict.passed, false);
  assert.match(verdict.failures.join("\n"), /never stopped answering.*proves nothing/);
});

test("the live runner passes when the run identity is unchanged", async () => {
  const { deps, replacementCalls, witnessHosts } = dependencies({ afterIdentity: { ...runningOn } });
  const verdict = await runJobSurvivesReplacement(liveOptions, deps);
  assert.equal(verdict.passed, true, verdict.failures.join("; "));
  assert.deepEqual(replacementCalls, [["aws", "update-service", "--force-new-deployment"]]);
  // The witness is read on the host the completed record names, not on an empty string.
  assert.deepEqual(witnessHosts, [runningOn.executionHost]);
});

test("the live runner reads the active job by job id, which is the only identifier it accepts", async () => {
  const namespaces: Array<{ namespace: string; payload: JsonValue }> = [];
  const { deps } = dependencies({ afterIdentity: { ...runningOn } });
  const wrapped: SurvivalDependencies = {
    ...deps,
    api: {
      async request(namespace, payload) {
        namespaces.push({ namespace, payload });
        return deps.api.request(namespace, payload);
      },
    },
  };
  await runJobSurvivesReplacement(liveOptions, wrapped);
  const active = namespaces.filter((entry) => entry.namespace === "Scheduler.GetActiveJob");
  assert.ok(active.length > 0);
  for (const entry of active) assert.deepEqual(entry.payload, { job_id: "3" });
  const completed = namespaces.filter((entry) => entry.namespace === "Scheduler.GetCompletedJob");
  assert.ok(completed.length > 0);
  for (const entry of completed) assert.deepEqual(entry.payload, { job_uid: "uid-3" });
});

test("the live runner catches a requeue on the start time", async () => {
  const { deps } = dependencies({
    afterIdentity: { ...runningOn, startTime: "2026-09-10T12:09:00Z" },
  });
  const verdict = await runJobSurvivesReplacement(liveOptions, deps);
  assert.equal(verdict.passed, false);
  assert.match(verdict.failures.join("\n"), /start time moved/);
});

test("the live runner catches a requeue on the witness alone, with no start time reported", async () => {
  const unreported: RunIdentity = { executionHost: "192.0.2.44", instanceId: "", startTime: "", state: "RUNNING" };
  const { deps } = dependencies({
    afterIdentity: { ...unreported },
    beforeIdentity: { ...unreported },
    exitStatus: WITNESS_RERUN_EXIT_STATUS,
    witnessReruns: 1,
  });
  const verdict = await runJobSurvivesReplacement(liveOptions, deps);
  assert.equal(verdict.passed, false);
  assert.match(verdict.failures.join("\n"), /ran 2 times: the run was requeued/);
  assert.match(verdict.failures.join("\n"), /exit status 17/);
});

test("the live runner fails when the replacement never took the control plane away", async () => {
  const { deps } = dependencies({ afterIdentity: { ...runningOn }, quietListings: 0 });
  const verdict = await runJobSurvivesReplacement(liveOptions, deps);
  assert.equal(verdict.passed, false);
  assert.match(verdict.failures.join("\n"), /never stopped answering/);
});

test("the live runner reports a failed replacement command without asserting on the job", async () => {
  const { deps } = dependencies({ afterIdentity: { ...runningOn }, replacementExit: 1 });
  const verdict = await runJobSurvivesReplacement(liveOptions, deps);
  assert.equal(verdict.passed, false);
  assert.match(verdict.failures.join("\n"), /replacement command failed with exit 1/);
});

test("the requeue window must be stated, and the replacement command must name an executable", () => {
  const base = [
    "--cluster-name",
    "sample-cluster",
    "--aws-region",
    "us-east-2",
    "--alb-host",
    "cluster.example.invalid",
    "--username",
    "tester",
    "--password-file",
    "/secure/password",
  ];
  assert.throws(
    () => parseSurvivalOptions([...base, "--replacement", "aws ecs update-service"]),
    /node-fail-requeue-seconds/,
  );
  const parsed = parseSurvivalOptions([
    ...base,
    "--replacement",
    "aws ecs update-service --force-new-deployment",
    "--node-fail-requeue-seconds",
    "600",
    "--job-sleep-seconds",
    "1800",
  ]);
  assert.equal(parsed.options.replacementExecutable, "aws");
  assert.deepEqual(parsed.options.replacementArgs, ["ecs", "update-service", "--force-new-deployment"]);
  assert.equal(parsed.options.nodeFailRequeueSeconds, 600);
  assert.equal(parsed.options.jobSleepSeconds, 1_800);
});
