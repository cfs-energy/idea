import assert from "node:assert/strict";
import test from "node:test";

import {
  parseSchedulerServerNameOptions,
  parseServerEvidence,
  runSchedulerServerNameCheck,
  schedulerServerNameFailures,
  serverProbeCommand,
  type SchedulerServerEvidence,
} from "../../tools/e2e/checks/scheduler-server-name.ts";

const configured = "scheduler.sample-cluster.us-east-2.local";

/** A server built after the stable-name row was turned on. */
const usesStableName: SchedulerServerEvidence = {
  hostsPin: "192.0.2.44",
  pbsServer: "scheduler",
  reportedServer: "scheduler",
  resolvedIpv4: "192.0.2.44",
  schedHost: "scheduler.sample-cluster.us-east-2.local",
};

/**
 * The stable-name row is on and `scheduler.private_dns_name` is set to the stable name: the
 * stable name resolves and the running server has never heard of it.
 */
const rowOnServerNot: SchedulerServerEvidence = {
  hostsPin: "",
  pbsServer: "ip-192-0-2-91",
  reportedServer: "ip-192-0-2-91",
  resolvedIpv4: "192.0.2.91",
  schedHost: "ip-192-0-2-91.us-east-2.compute.internal",
};

test("a server that uses the configured name passes", () => {
  assert.deepEqual(schedulerServerNameFailures(configured, usesStableName), []);
});

test("the setting being on does not pass a server that does not use it", () => {
  const failures = schedulerServerNameFailures(configured, rowOnServerNot);
  assert.equal(failures.length, 2);
  // Both values in every refusal: the row alone is not actionable.
  for (const failure of failures) {
    assert.match(failure, new RegExp(configured));
    assert.match(failure, /ip-192-0-2-91/);
  }
  assert.match(failures.join("\n"), /Nothing re-renders an existing host/);
});

test("a batch server that does not answer is a refusal, not a pass", () => {
  const failures = schedulerServerNameFailures(configured, { ...usesStableName, reportedServer: "" });
  assert.match(failures.join("\n"), /did not answer qstat -B/);
});

test("a stable name that does not resolve is a refusal", () => {
  const failures = schedulerServerNameFailures(configured, { ...usesStableName, resolvedIpv4: "" });
  assert.match(failures.join("\n"), /does not resolve to an IPv4 address/);
});

test("the probe reads only, and refuses a name that is not a host name", () => {
  const command = serverProbeCommand(configured);
  assert.match(command, /PBS_SERVER/);
  assert.match(command, /qstat -B -f/);
  assert.match(command, /qmgr -c "print sched"/);
  assert.doesNotMatch(command, /qmgr -c "set|qalter|qterm|qrerun|qdel|qhold|qsub/);
  // Every redirection goes to /dev/null: the probe writes no file on the scheduler.
  assert.deepEqual(
    [...command.matchAll(/>\s*(\S+)/g)].map((match) => match[1]),
    ["/dev/null", "/dev/null", "/dev/null", "/dev/null", "/dev/null"],
  );
  assert.throws(() => serverProbeCommand("scheduler.local; rm -rf /"), /must be a host name/);
});

test("absent probe labels read as missing, never as agreement", () => {
  assert.deepEqual(parseServerEvidence("noise\nPBS_SERVER=scheduler\n"), {
    hostsPin: "",
    pbsServer: "scheduler",
    reportedServer: "",
    resolvedIpv4: "",
    schedHost: "",
  });
});

test("the check refuses probe output whose server name is the instance name", async () => {
  const result = await runSchedulerServerNameCheck(
    { configuredServerName: configured, schedulerHost: "192.0.2.91" },
    {
      async run() {
        return {
          exitCode: 0,
          stderr: "",
          stdout: [
            "PBS_SERVER=ip-192-0-2-91",
            "REPORTED=ip-192-0-2-91",
            "SCHED_HOST=ip-192-0-2-91.us-east-2.compute.internal",
            "RESOLVED_IPV4=192.0.2.91",
            "HOSTS_PIN=",
          ].join("\n"),
        };
      },
    },
  );
  assert.equal(result.passed, false);
  assert.ok(result.observed.includes(`configured_server=${configured}`));
  assert.ok(result.observed.includes("pbs_server=ip-192-0-2-91"));
  assert.ok(result.observed.includes("hosts_pin=none"));
});

test("a probe that could not run is a refusal", async () => {
  const result = await runSchedulerServerNameCheck(
    { configuredServerName: configured, schedulerHost: "192.0.2.91" },
    {
      async run() {
        return { exitCode: 255, stderr: "Connection refused", stdout: "" };
      },
    },
  );
  assert.equal(result.passed, false);
  assert.match(result.observed.join("\n"), /probe_exit=255/);
});

test("both the scheduler host and the configured name must be supplied", () => {
  assert.throws(
    () => parseSchedulerServerNameOptions(["--scheduler-host", "192.0.2.91", "--ssh-user", "operator"]),
    /configured-server/,
  );
  const parsed = parseSchedulerServerNameOptions([
    "--configured-server",
    configured,
    "--scheduler-host",
    "192.0.2.91",
    "--ssh-user",
    "operator",
  ]);
  assert.equal(parsed.check.configuredServerName, configured);
  assert.equal(parsed.ssh.user, "operator");
});
