/**
 * The read-only batch server probe.
 *
 * The fixtures are the shape a real server answers with, taken from a live read,
 * and every refusal is checked in both directions: an answer that establishes the
 * state must pass the same check that an unestablished one fails.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  SCHEDULER_READ_DOCUMENT_CONTENT,
  SCHEDULER_READ_DOCUMENT_NAME,
  SchedulerStateUnreadableError,
  ensureSchedulerReadDocument,
  parseBatchServerState,
  readBatchServerState,
  renderBatchServerState,
  type SsmInvocation,
  type SsmReadChannel,
} from "../../src/cli/scheduler-state-read.ts";

/** Server and queue replies in the shape `qstat -B -f -F json` and `qstat -Q -f -F json` return. */
function probeOutput(options: {
  scheduling?: string;
  stateCount?: string;
  totalJobs?: number;
  queues?: Record<string, { enabled: string; started: string }>;
} = {}): string {
  const server = {
    timestamp: 1_789_129_826,
    pbs_version: "23.06.06",
    pbs_server: "ip-192-0-2-10",
    Server: {
      "ip-192-0-2-10": {
        server_state: "Active",
        scheduling: options.scheduling ?? "True",
        // Job history keeps finished jobs in the total, so a drained server still reports some.
        total_jobs: options.totalJobs ?? 4,
        state_count: options.stateCount ?? "Transit:0 Queued:0 Held:0 Waiting:0 Running:0 Exiting:0 Begun:0 ",
      },
    },
  };
  const queues = {
    timestamp: 1_789_129_827,
    Queue: options.queues ?? {
      workq: { enabled: "True", started: "True" },
      normal: { enabled: "True", started: "True" },
    },
  };
  return `${JSON.stringify(server)}\n---QUEUES---\n${JSON.stringify(queues)}\n`;
}

test("the probe output yields the scheduling flag, every queue and the live job counts", () => {
  const state = parseBatchServerState(probeOutput());

  assert.equal(state.serverName, "ip-192-0-2-10");
  assert.equal(state.scheduling, true);
  assert.deepEqual(state.queues, [
    { name: "workq", enabled: true, started: true },
    { name: "normal", enabled: true, started: true },
  ]);
  assert.equal(state.queuedJobs, 0);
  assert.equal(state.provisioningJobs, 0);
  assert.equal(state.runningJobs, 0);
});

test("the job count comes from the live states, not from the history-inflated total", () => {
  // A real drained development server reports total_jobs 4 with every state at zero, because
  // job history is enabled. Reading the total would refuse a drained cluster for ever.
  const drained = parseBatchServerState(probeOutput({ totalJobs: 4 }));
  assert.equal(drained.queuedJobs + drained.provisioningJobs + drained.runningJobs, 0);

  const busy = parseBatchServerState(
    probeOutput({ totalJobs: 4, stateCount: "Transit:1 Queued:2 Held:3 Waiting:4 Running:5 Exiting:6 Begun:7" }),
  );
  assert.equal(busy.queuedJobs, 10, "queued, waiting, transit and held");
  assert.equal(busy.provisioningJobs, 7);
  assert.equal(busy.runningJobs, 11, "running and exiting");
});

test("a closed server reads as closed", () => {
  const state = parseBatchServerState(
    probeOutput({
      scheduling: "False",
      queues: { normal: { enabled: "False", started: "False" } },
    }),
  );
  assert.equal(state.scheduling, false);
  assert.deepEqual(state.queues.filter((queue) => queue.enabled), []);
  assert.match(renderBatchServerState(state), /scheduling=false; queues=1; enabled=none/);
});

test("every unreadable answer throws rather than defaulting to a closed scheduler", () => {
  const unreadable: Array<[string, string]> = [
    ["no queue section", JSON.stringify({ Server: {} })],
    ["server reply is not JSON", `not json\n---QUEUES---\n{"Queue":{}}`],
    ["queue reply is not JSON", `{"Server":{}}\n---QUEUES---\nnot json`],
    ["no server named", `{"Server":{}}\n---QUEUES---\n{"Queue":{}}`],
    [
      "no scheduling attribute",
      `{"Server":{"s":{"state_count":"Queued:0"}}}\n---QUEUES---\n{"Queue":{}}`,
    ],
    [
      "scheduling is not a flag",
      `{"Server":{"s":{"scheduling":"maybe","state_count":"Queued:0"}}}\n---QUEUES---\n{"Queue":{}}`,
    ],
    [
      "no state_count",
      `{"Server":{"s":{"scheduling":"False"}}}\n---QUEUES---\n{"Queue":{}}`,
    ],
    [
      "an unreadable state_count field",
      `{"Server":{"s":{"scheduling":"False","state_count":"Queued:many"}}}\n---QUEUES---\n{"Queue":{}}`,
    ],
    [
      "no queue list",
      `{"Server":{"s":{"scheduling":"False","state_count":"Queued:0"}}}\n---QUEUES---\n{}`,
    ],
    [
      "a queue without an enabled flag",
      `{"Server":{"s":{"scheduling":"False","state_count":"Queued:0"}}}\n---QUEUES---\n{"Queue":{"normal":{"started":"True"}}}`,
    ],
  ];
  for (const [label, stdout] of unreadable) {
    assert.throws(
      () => parseBatchServerState(stdout),
      SchedulerStateUnreadableError,
      `${label} did not throw`,
    );
  }
  // The control: the same parser accepts an answer that does establish the state.
  assert.equal(parseBatchServerState(probeOutput({ scheduling: "False" })).scheduling, false);
});

/** A channel that records what it was asked to do. */
function fakeChannel(options: {
  stored?: string;
  invocation?: Partial<SsmInvocation>;
} = {}): SsmReadChannel & { created: Array<{ name: string; content: string }>; ran: string[] } {
  let stored = options.stored;
  const created: Array<{ name: string; content: string }> = [];
  const ran: string[] = [];
  return {
    created,
    ran,
    async documentContent() {
      return stored;
    },
    async createDocument(name, content) {
      created.push({ name, content });
      stored = content;
    },
    async runDocument(name) {
      ran.push(name);
      return {
        status: "Success",
        responseCode: 0,
        stdout: probeOutput({ scheduling: "False", queues: { normal: { enabled: "False", started: "False" } } }),
        stderr: "",
        ...options.invocation,
      };
    },
  };
}

test("the document is created when absent and its name carries the content digest", async () => {
  const channel = fakeChannel();
  assert.equal(await ensureSchedulerReadDocument(channel), "created");
  assert.deepEqual(channel.created, [
    { name: SCHEDULER_READ_DOCUMENT_NAME, content: SCHEDULER_READ_DOCUMENT_CONTENT },
  ]);
  assert.match(SCHEDULER_READ_DOCUMENT_NAME, /^idea-scheduler-read-state-[0-9a-f]{12}$/);
});

test("a document already in place with this release's content is used as it stands", async () => {
  const channel = fakeChannel({ stored: SCHEDULER_READ_DOCUMENT_CONTENT });
  assert.equal(await ensureSchedulerReadDocument(channel), "present");
  assert.deepEqual(channel.created, []);
});

test("a document whose content this release did not write is refused, not run", async () => {
  const writing = JSON.stringify({
    schemaVersion: "2.2",
    mainSteps: [
      {
        action: "aws:runShellScript",
        name: "readBatchServerState",
        inputs: { runCommand: ["/opt/pbs/bin/qmgr -c 'set server scheduling = False'"] },
      },
    ],
  });
  const channel = fakeChannel({ stored: writing });

  await assert.rejects(ensureSchedulerReadDocument(channel), (error: unknown) => {
    assert.ok(error instanceof SchedulerStateUnreadableError);
    assert.match(error.message, /exists with content this release did not write/);
    return true;
  });
  assert.deepEqual(channel.ran, [], "a document that was not verified must not be run");
});

test("the document this release writes contains no parameter and no write", () => {
  const document = JSON.parse(SCHEDULER_READ_DOCUMENT_CONTENT) as Record<string, unknown>;
  assert.equal(document["parameters"], undefined, "a parameter is a way to put text on the host");
  const commands = (document["mainSteps"] as Array<{ inputs: { runCommand: string[] } }>)
    .flatMap((step) => step.inputs.runCommand);
  for (const command of commands) {
    assert.ok(
      /^(set -eu|echo ---QUEUES---|\/opt\/pbs\/bin\/qstat )/.test(command),
      `the document runs something other than a read: ${command}`,
    );
  }
  assert.ok(commands.some((command) => command.includes("qstat -B")));
  assert.ok(commands.some((command) => command.includes("qstat -Q")));
});

test("a failed invocation throws instead of reporting an empty scheduler", async () => {
  for (const invocation of [
    { status: "Failed", responseCode: 1, stderr: "qstat: cannot connect to server" },
    { status: "TimedOut", responseCode: -1 },
    { status: "Success", responseCode: 2 },
  ]) {
    await assert.rejects(
      readBatchServerState(fakeChannel({ invocation }), "i-0000000000000001"),
      SchedulerStateUnreadableError,
      `${invocation.status}/${invocation.responseCode} did not throw`,
    );
  }
  // The control: a successful invocation of the same shape does establish the state.
  const state = await readBatchServerState(fakeChannel(), "i-0000000000000001");
  assert.equal(state.scheduling, false);
});
