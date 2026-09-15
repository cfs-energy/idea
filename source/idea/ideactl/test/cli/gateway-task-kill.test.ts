/**
 * The gateway task-kill check's two passing outcomes and its failing one, against a fake context.
 * A flow through the network load balancer is pinned to one task, so the connection opened before
 * the kill either survives or closes; a closed one must be replaceable once the service recovers.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { gatewayTaskKillCheck } from "../../tools/e2e/checks/gateway-task-kill.ts";
import type { JsonValue } from "../../tools/e2e/api.ts";
import type { CheckContext, GatewayConnection, ProofMatrixOptions } from "../../tools/e2e/checks/types.ts";

const options: ProofMatrixOptions = {
  albHost: "control.example.invalid",
  checks: ["gateway-task-kill"],
  cluster: "sample-containers",
  desktopRequest: { session: { name: "proof-desktop" } },
  gatewayHost: "gateway.example.invalid",
  gatewayService: "sample-gateway",
  gatewayTargetGroup: "tg-sample",
  gatewayTask: "task-1",
  insecureTls: false,
  passwordFile: "/secure/password",
  pollSeconds: 1,
  readyTimeoutSeconds: 60,
  region: "us-east-2",
  replacementTimeoutSeconds: 60,
  username: "tester",
};

/** The fake gateway: the first connection dies with the stopped task; later ones behave as told. */
function context(reconnect: "opens" | "closed"): { context: CheckContext; output: string[] } {
  let clock = 1_000_000;
  let stopped = false;
  let connections = 0;
  const output: string[] = [];
  const session = { idea_session_id: "s-1", owner: "tester", name: "proof-desktop", state: "READY" };
  const connection = (open: () => boolean): GatewayConnection => ({ close() {}, isOpen: open });
  return {
    output,
    context: {
      api: {
        async request(namespace): Promise<{ body: JsonValue; status: number }> {
          const payload: JsonValue = namespace.endsWith("Session") || namespace.endsWith("SessionInfo") ? { session } : {};
          return { status: 200, body: { success: true, payload } };
        },
      },
      cloud: {
        async healthyTargetCount() { return 2; },
        async serviceRunningCount() { return { desired: 2, running: 2 }; },
        async stopTask() { stopped = true; },
      },
      gateway: {
        async connect() {
          connections += 1;
          if (connections === 1) return connection(() => !stopped);
          return connection(() => reconnect === "opens");
        },
      },
      now: () => clock,
      options,
      output: (line) => output.push(line),
      processes: { async run() { throw new Error("no processes"); } },
      async sleep(milliseconds) { clock += milliseconds; },
    },
  };
}

test("a connection that closes with its task passes when a new one opens after recovery", async () => {
  const harness = context("opens");
  const result = await gatewayTaskKillCheck.run(harness.context);
  assert.ok(result.passed, result.observed.join("\n"));
  assert.ok(result.observed.some((line) => line.startsWith("connection closed during gateway replacement; a new connection opened in")));
  assert.ok(harness.output.some((line) => line.startsWith("ACTION the connection closed during gateway replacement")));
});

test("a closed connection whose replacement does not open fails", async () => {
  const result = await gatewayTaskKillCheck.run(context("closed").context);
  assert.ok(!result.passed);
  assert.ok(result.observed.some((line) => line.includes("connection closed during gateway replacement and a new one failed: gateway connection closed before verification")), result.observed.join("\n"));
});
