/** The desktop-stream check against a fake portal and gateway: a confirmed session, an abort, and a closed socket. */

import assert from "node:assert/strict";
import test from "node:test";

import type { JsonValue } from "../../tools/e2e/api.ts";
import { desktopStreamCheck } from "../../tools/e2e/checks/desktop-stream.ts";
import type { CheckContext, DcvSessionOutcome, ProofMatrixOptions } from "../../tools/e2e/checks/types.ts";

const options: ProofMatrixOptions = {
  albHost: "control.example.invalid",
  checks: ["desktop-stream"],
  desktopRequest: { session: { name: "proof-desktop" } },
  insecureTls: false,
  passwordFile: "/secure/password",
  pollSeconds: 1,
  readyTimeoutSeconds: 60,
  username: "tester",
};

function context(outcome: DcvSessionOutcome | Error): { context: CheckContext; opened: Array<{ url: string; sessionId: string }>; } {
  let clock = 1_000_000;
  const opened: Array<{ url: string; sessionId: string }> = [];
  const session = { idea_session_id: "s-1", owner: "tester", name: "proof-desktop", state: "READY" };
  return {
    opened,
    context: {
      api: {
        async request(namespace): Promise<{ body: JsonValue; status: number }> {
          if (namespace === "VirtualDesktop.GetSessionConnectionInfo") {
            return { status: 200, body: { success: true, payload: { connection_info: { endpoint: "https://gateway.example.invalid", web_url_path: "/", access_token: "tok", dcv_session_id: "dcv-1" } } } };
          }
          const payload: JsonValue = namespace.endsWith("Session") || namespace.endsWith("SessionInfo") ? { session } : {};
          return { status: 200, body: { success: true, payload } };
        },
      },
      cloud: { async healthyTargetCount() { return 0; }, async serviceRunningCount() { return { desired: 0, running: 0 }; }, async stopTask() {} },
      gateway: {
        async connect() { throw new Error("no raw gateway"); },
        async openSession(input) {
          opened.push({ url: input.url, sessionId: input.sessionId });
          if (outcome instanceof Error) throw outcome;
          return outcome;
        },
      },
      now: () => clock,
      options,
      output: () => {},
      processes: { async run() { throw new Error("no processes"); } },
      async sleep(milliseconds) { clock += milliseconds; },
    },
  };
}

test("a confirmed DCV session passes and names the server", async () => {
  const harness = context({ elapsedMs: 812, reply: { kind: "confirm", connectionId: 3, serverName: "NICE DCV Server" } });
  const result = await desktopStreamCheck.run(harness.context);
  assert.ok(result.passed, result.observed.join("\n"));
  assert.ok(result.observed.some((line) => line === "session confirmed by NICE DCV Server (connection 3) in 812ms"));
  assert.deepEqual(harness.opened, [{ url: "wss://gateway.example.invalid/ws", sessionId: "dcv-1" }]);
});

test("an abort from the gateway fails with its reason", async () => {
  const result = await desktopStreamCheck.run(context({ elapsedMs: 10_000, reply: { kind: "abort", reason: 40, reasonName: "SERVER_UNREACHABLE" } }).context);
  assert.ok(!result.passed);
  assert.ok(result.observed.some((line) => line === "the gateway aborted the session: SERVER_UNREACHABLE"), result.observed.join("\n"));
});

test("a socket closed before any reply fails with the close reason", async () => {
  const result = await desktopStreamCheck.run(context(new Error("closed with code 1006")).context);
  assert.ok(!result.passed);
  assert.ok(result.observed.some((line) => line.includes("closed the session before answering: closed with code 1006")));
});
