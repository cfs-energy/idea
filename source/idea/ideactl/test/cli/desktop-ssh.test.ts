/** The desktop-ssh check against a fake portal, desktop and ssh: a retry that succeeds, and a run that never does. */

import assert from "node:assert/strict";
import test from "node:test";

import type { JsonValue } from "../../tools/e2e/api.ts";
import { desktopSshCheck } from "../../tools/e2e/checks/desktop-ssh.ts";
import type { CheckContext, ProofMatrixOptions } from "../../tools/e2e/checks/types.ts";

const options: ProofMatrixOptions = {
  albHost: "control.example.invalid",
  bastionHost: "203.0.113.10",
  checks: ["desktop-ssh"],
  desktopRequest: { session: { name: "proof-desktop" } },
  insecureTls: false,
  passwordFile: "/secure/password",
  pollSeconds: 1,
  readyTimeoutSeconds: 60,
  username: "tester",
};

function context(sshExitCodes: number[]): { context: CheckContext; runs: string[][]; output: string[] } {
  let clock = 1_000_000;
  const runs: string[][] = [];
  const output: string[] = [];
  const session = { idea_session_id: "s-1", owner: "tester", name: "proof-desktop", state: "READY", server: { private_ip: "10.0.0.5" } };
  return {
    runs,
    output,
    context: {
      api: {
        async request(namespace): Promise<{ body: JsonValue; status: number }> {
          if (namespace === "Auth.GetUserPrivateKey") return { status: 200, body: { success: true, payload: { name: "tester", key_material: "-----BEGIN KEY-----\nabc\n-----END KEY-----\n" } } };
          const payload: JsonValue = namespace.endsWith("Session") || namespace.endsWith("SessionInfo") ? { session } : {};
          return { status: 200, body: { success: true, payload } };
        },
      },
      cloud: {
        async healthyTargetCount() { return 0; },
        async serviceRunningCount() { return { desired: 0, running: 0 }; },
        async stopTask() {},
      },
      gateway: { async connect() { throw new Error("no gateway"); } },
      now: () => clock,
      options,
      output: (line) => output.push(line),
      processes: {
        async run(command, args) {
          runs.push([command, ...args]);
          const exitCode = sshExitCodes[runs.length - 1] ?? 255;
          return { exitCode, stdout: exitCode === 0 ? "ip-10-0-0-5\ntester\n" : "", stderr: exitCode === 0 ? "" : "Permission denied (publickey)." };
        },
      },
      async sleep(milliseconds) { clock += milliseconds; },
    },
  };
}

test("ssh through the bastion with the portal key passes, retrying once for a trailing directory client", async () => {
  const harness = context([255, 0]);
  const result = await desktopSshCheck.run(harness.context);
  assert.ok(result.passed, result.observed.join("\n"));
  assert.ok(result.observed.some((line) => line.startsWith("ssh succeeded on attempt 2 in 60000ms: ip-10-0-0-5 tester")));
  const args = harness.runs[0] ?? [];
  assert.equal(args[0], "ssh");
  assert.equal(args.at(-2), "tester@10.0.0.5");
  assert.ok(args.some((arg) => arg.startsWith("ProxyCommand=ssh ") && arg.endsWith("-W %h:%p tester@203.0.113.10")));
  assert.ok(args.includes("BatchMode=yes"));
  assert.ok(harness.output.some((line) => line === "ACTION ssh tester@10.0.0.5 through 203.0.113.10"));
});

test("three refused attempts fail with the last stderr", async () => {
  const harness = context([255, 255, 255]);
  const result = await desktopSshCheck.run(harness.context);
  assert.ok(!result.passed);
  assert.equal(harness.runs.length, 3);
  assert.ok(result.observed.some((line) => line.includes("ssh failed 3 times, exit 255: Permission denied (publickey).")), result.observed.join("\n"));
});
