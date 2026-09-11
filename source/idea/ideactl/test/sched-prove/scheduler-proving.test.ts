import assert from "node:assert/strict";
import test from "node:test";

import type { ApiCallResult, JsonValue } from "../../tools/e2e/api.ts";
import {
  parseNodeEvidence,
  parseStableSchedulerNameOptions,
  runStableSchedulerNameCheck,
} from "../../tools/e2e/checks/scheduler-stable-name.ts";
import {
  parseSchedulerVmReplacementOptions,
  runSchedulerVmReplacementCheck,
  type SchedulerVmReplacementDependencies,
} from "../../tools/e2e/checks/scheduler-vm-replacement.ts";

const stableName = "scheduler.sample-cluster.us-east-2.local";

/** Builds a successful scheduler API result around a JSON payload. */
function response(payload: JsonValue): ApiCallResult {
  return { body: { payload, success: true }, status: 200 };
}

test("accepts only clients and compute nodes using the stable scheduler name", async () => {
  const commands: string[] = [];
  const result = await runStableSchedulerNameCheck(
    {
      expectedAddress: "192.0.2.44",
      expectedServer: stableName,
      nodes: [
        { host: "bastion.example.invalid", name: "client" },
        { host: "compute.example.invalid", name: "compute-1" },
      ],
    },
    {
      async run(host, command) {
        commands.push(`${host}:${command}`);
        return {
          exitCode: 0,
          stderr: "",
          stdout: [
            `PBS_SERVER=${stableName}`,
            "HOSTS_PIN=",
            "RESOLVED_IPV4=192.0.2.44",
            "QSTAT_B=OK",
          ].join("\n"),
        };
      },
    },
  );

  assert.equal(result.passed, true);
  assert.equal(commands.length, 2);
  assert.match(commands[0] ?? "", /getent ahostsv4/);
  assert.deepEqual(result.observed, [
    `node=client pbs_server=${stableName} hosts_pin=none resolved_ipv4=192.0.2.44 qstat_b=OK`,
    `node=compute-1 pbs_server=${stableName} hosts_pin=none resolved_ipv4=192.0.2.44 qstat_b=OK`,
  ]);
});

test("rejects an old compute-node address pin", async () => {
  const result = await runStableSchedulerNameCheck(
    {
      expectedServer: stableName,
      nodes: [{ host: "compute.example.invalid", name: "compute-1" }],
    },
    {
      async run() {
        return {
          exitCode: 0,
          stderr: "",
          stdout: [
            `PBS_SERVER=${stableName}`,
            "HOSTS_PIN=192.0.2.13",
            "RESOLVED_IPV4=192.0.2.44",
            "QSTAT_B=OK",
          ].join("\n"),
        };
      },
    },
  );

  assert.equal(result.passed, false);
  assert.ok(result.observed.includes("node=compute-1 pinned_address=192.0.2.13"));
});

test("parses labelled probe output and rejects malformed node flags", () => {
  assert.deepEqual(
    parseNodeEvidence(`ignored=value\nPBS_SERVER=${stableName}\nHOSTS_PIN=\nRESOLVED_IPV4=192.0.2.44\nQSTAT_B=OK\n`),
    {
      hostsPin: "",
      pbsServer: stableName,
      qstat: "OK",
      resolvedIpv4: "192.0.2.44",
    },
  );
  assert.throws(
    () => parseStableSchedulerNameOptions(["--expected-server", stableName, "--ssh-user", "operator", "--node", "missing-host"]),
    /label=host/,
  );
});

test("requires a new scheduler machine identity and the original job completion", async () => {
  let identityReads = 0;
  const calls: string[] = [];
  // The run identity the batch server reports, unchanged across the replacement: the same run,
  // on the same node. A requeue is covered in test/acceptance/replacement-checks.test.ts.
  const activeJob = {
    execution_hosts: [{ host: "192.0.2.44", instance_id: "i-0123456789abcdef0" }],
    start_time: "2026-09-11T03:12:37Z",
    state: "running",
  };
  const dependencies: SchedulerVmReplacementDependencies = {
    api: {
      async request(namespace: string): Promise<ApiCallResult> {
        calls.push(namespace);
        if (namespace === "Scheduler.SubmitJob") {
          return response({ job: { job_id: "1", job_uid: "uid-1" } });
        }
        if (namespace === "Scheduler.GetActiveJob") {
          return response({ job: activeJob });
        }
        if (namespace === "Scheduler.GetCompletedJob") {
          return response({ job: { exit_status: 0 } });
        }
        throw new Error(`unexpected scheduler request: ${namespace}`);
      },
    },
    now: () => 1_000,
    remote: {
      async run(_host: string, command: string) {
        if (command === "cat /etc/machine-id") {
          identityReads += 1;
          return {
            exitCode: 0,
            stderr: "",
            stdout: identityReads === 1 ? "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n" : "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n",
          };
        }
        if (command === '/opt/pbs/bin/qmgr -c "list queue"') {
          return { exitCode: 0, stderr: "", stdout: "Queue normal\nQueue priority\n" };
        }
        assert.equal(command, "/opt/pbs/bin/qstat -B >/dev/null 2>&1");
        return { exitCode: 0, stderr: "", stdout: "" };
      },
    },
    replacement: {
      async run(executable, args) {
        assert.equal(executable, "ideactl");
        assert.deepEqual(args, ["deploy", "--upgrade", "scheduler"]);
        return { exitCode: 0, stderr: "", stdout: "deployment complete\n" };
      },
    },
    sleep: async () => {},
  };

  const result = await runSchedulerVmReplacementCheck(
    {
      expectedExitStatus: 0,
      jobSleepSeconds: 3600,
      pollSeconds: 1,
      replacementArgs: ["deploy", "--upgrade", "scheduler"],
      replacementExecutable: "ideactl",
      schedulerHost: stableName,
      timeoutSeconds: 10,
    },
    dependencies,
  );

  assert.equal(result.passed, true);
  assert.deepEqual(calls, [
    "Scheduler.SubmitJob",
    "Scheduler.GetActiveJob",
    "Scheduler.GetActiveJob",
    "Scheduler.GetCompletedJob",
  ]);
  assert.ok(result.observed.includes("scheduler_identity_before=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"));
  assert.ok(result.observed.includes("scheduler_identity_after=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"));
  assert.ok(result.observed.includes("queues_before=normal,priority"));
  assert.ok(result.observed.includes("queues_after=normal,priority"));
  assert.ok(result.observed.includes("job=uid-1 completed_exit_status=0"));
  assert.ok(result.observed.includes("start_time_after=2026-09-11T03:12:37Z"));
});

test("parses option-like replacement arguments without invoking a shell", () => {
  const parsed = parseSchedulerVmReplacementOptions([
    "--alb-host",
    "control.example.invalid",
    "--username",
    "operator",
    "--password-file",
    "/tmp/password",
    "--scheduler-host",
    stableName,
    "--ssh-user",
    "operator",
    "--job-sleep-seconds",
    "3600",
    "--replace-executable",
    "ideactl",
    "--replace-arg",
    "deploy",
    "--replace-arg",
    "--upgrade",
    "--replace-arg",
    "scheduler",
    "--insecure",
  ]);

  assert.deepEqual(parsed.check.replacementArgs, ["deploy", "--upgrade", "scheduler"]);
  assert.equal(parsed.api.insecureTls, true);
  assert.equal(parsed.check.timeoutSeconds, 1800);
});
