import assert from "node:assert/strict";
import test from "node:test";
import {
  missingRequiredFlags,
  parseProofMatrixOptions,
  runProofMatrix,
} from "../../tools/e2e/proof-matrix.ts";
import type { JsonValue } from "../../tools/e2e/api.ts";
import type { ProofMatrixOptions } from "../../tools/e2e/checks/types.ts";

/** Builds a complete API response value for injected API tests. */
function response(payload: JsonValue): { body: JsonValue; status: number } {
  return { body: { payload, success: true }, status: 200 };
}

/** Supplies safe, injected dependencies that make no network calls. */
function dependencies(processOutput: string) {
  const events: string[] = [];
  const calls: string[] = [];
  return {
    api: {
      async request(namespace: string): Promise<{ body: JsonValue; status: number }> {
        calls.push(namespace);
        if (namespace === "VirtualDesktop.CreateSession") {
          return response({ session: { idea_session_id: "session-1", state: "CREATING" } });
        }
        if (namespace === "VirtualDesktop.GetSessionInfo") {
          return response({ session: { idea_session_id: "session-1", state: "READY" } });
        }
        if (namespace === "VirtualDesktop.GetSessionConnectionInfo") {
          return response({ connection_info: { endpoint: "gateway.example.invalid" } });
        }
        if (namespace === "VirtualDesktop.DeleteSessions") {
          return response({ success: [{}] });
        }
        throw new Error(`unexpected API request: ${namespace}`);
      },
    },
    calls,
    cloud: {
      async healthyTargetCount(): Promise<number> {
        return 2;
      },
      async serviceRunningCount(): Promise<{ desired: number; running: number }> {
        return { desired: 2, running: 2 };
      },
      async stopTask(): Promise<void> {
        calls.push("stop-task");
      },
    },
    gateway: {
      async connect() {
        let open = true;
        return {
          close: () => {
            open = false;
          },
          isOpen: () => open,
        };
      },
    },
    now: () => 1_000,
    output: (line: string) => events.push(line),
    processes: {
      async run(_command: string, args: string[]) {
        const script = args[0];
        if (script !== undefined) {
          calls.push(script);
        }
        return { exitCode: 0, stderr: "", stdout: processOutput };
      },
    },
    sleep: async () => {},
    events,
  };
}

test("selects explicit checks and resolves environment values", () => {
  const options = parseProofMatrixOptions(
    ["--check", "job-burst,api-load", "--job-count", "4", "--api-max-p95-ms", "90", "--api-max-error-count", "0"],
    {
      IDEA_E2E_ALB_HOST: "control.example.invalid",
      IDEA_E2E_PASSWORD_FILE: "/tmp/password",
      IDEA_E2E_USERNAME: "test-user",
    },
  );

  assert.deepEqual(options.checks, ["job-burst", "api-load"]);
  assert.equal(options.jobCount, 4);
  assert.equal(options.apiMaxP95Ms, 90);
  assert.equal(options.albHost, "control.example.invalid");
  assert.equal(options.passwordFile, "/tmp/password");
  assert.equal(options.username, "test-user");
  assert.deepEqual(missingRequiredFlags(options), []);
});

test("requires only the selected check's flags", () => {
  const options = parseProofMatrixOptions(["--check", "gateway-load"]);

  assert.deepEqual(missingRequiredFlags(options), [
    "gateway-host",
    "gateway-max-p95-ms",
    "gateway-max-failures",
  ]);
  assert.throws(
    () => parseProofMatrixOptions(["--check", "not-a-check"]),
    /unknown check/,
  );
  assert.throws(
    () => parseProofMatrixOptions(["--check", "desktop-end-to-end", "--desktop-request", "[]"]),
    /JSON object/,
  );
});

test("aggregates passing and failing checks into a nonzero exit code", async () => {
  const passing: ProofMatrixOptions = {
    albHost: "control.example.invalid",
    apiMaxErrorCount: 0,
    apiMaxP95Ms: 90,
    checks: ["api-load"],
    insecureTls: false,
    passwordFile: "/tmp/password",
    username: "test-user",
  };
  const passingDependencies = dependencies("DONE requests=4 rate=1.0/s p50=10ms p95=50ms p99=60ms max=70ms errors={}\n");
  const passed = await runProofMatrix(passing, passingDependencies);
  assert.equal(passed.exitCode, 0);
  assert.deepEqual(passingDependencies.events.slice(-1), ["PASS api-load"]);
  assert.deepEqual(passingDependencies.calls, ["tools/e2e/load-api.ts"]);
  assert.deepEqual(passed.results[0]?.result.observed, [
    "exit=0",
    "p95=50ms errors=0",
    "p95_limit=90ms error_limit=0",
  ]);

  const overLimitDependencies = dependencies("DONE requests=4 rate=1.0/s p50=10ms p95=200ms p99=210ms max=220ms errors={}\n");
  const overLimit = await runProofMatrix(passing, overLimitDependencies);
  assert.equal(overLimit.exitCode, 1);
  assert.deepEqual(overLimitDependencies.calls, ["tools/e2e/load-api.ts"]);
  assert.deepEqual(overLimit.results[0]?.result.observed, [
    "exit=0",
    "p95=200ms errors=0",
    "p95_limit=90ms error_limit=0",
  ]);

  const failing: ProofMatrixOptions = {
    ...passing,
    checks: ["gateway-load"],
    gatewayHost: "gateway.example.invalid",
    gatewayMaxFailures: 0,
    gatewayMaxP95Ms: 90,
  };
  const failingDependencies = dependencies("DONE opened=2 failed={\"Timeout\":1} handshake_p50=10ms p95=20ms max=30ms\n");
  const failed = await runProofMatrix(failing, failingDependencies);
  assert.equal(failed.exitCode, 1);
  assert.deepEqual(failingDependencies.events.slice(-1), ["FAIL gateway-load"]);
  assert.deepEqual(failingDependencies.calls, ["tools/e2e/load-gateway.ts"]);
  assert.deepEqual(failed.results[0]?.result.observed, [
    "exit=0",
    "DONE opened=2 failed={\"Timeout\":1} handshake_p50=10ms p95=20ms max=30ms",
    "p95_limit=90ms failure_limit=0",
  ]);
});

test("uses injected API, cloud, and gateway clients for task replacement", async () => {
  const options: ProofMatrixOptions = {
    albHost: "control.example.invalid",
    brokerService: "broker-service",
    brokerTargetGroup: "broker-target-group",
    brokerTask: "broker-task",
    checks: ["broker-task-kill"],
    cluster: "sample-cluster",
    desktopRequest: { session: { name: "proof-desktop", project: { project_id: "proof-project" } } },
    gatewayHost: "gateway.example.invalid",
    insecureTls: false,
    passwordFile: "/tmp/password",
    username: "test-user",
  };
  const injected = dependencies("");
  const run = await runProofMatrix(options, injected);

  assert.equal(run.exitCode, 0);
  assert.deepEqual(injected.calls, [
    "VirtualDesktop.CreateSession",
    "VirtualDesktop.GetSessionInfo",
    "stop-task",
    "VirtualDesktop.GetSessionConnectionInfo",
    "VirtualDesktop.DeleteSessions",
  ]);
  assert.ok(injected.events.includes("PASS broker-task-kill"));
  assert.ok(run.results[0]?.result.observed.includes("connection remained open"));
});

test("account reconciliation reports NOT RUN without a writable directory", async () => {
  const deps = dependencies("");
  const run = await runProofMatrix(parseProofMatrixOptions(["--check", "account-reconcile"]), deps);
  assert.equal(run.results[0]?.result.skipped, true);
  assert.ok(deps.events.includes("NOT RUN account-reconcile"));
  assert.deepEqual(deps.calls, []);
});

test("account reconciliation disables via LDAP, witnesses STOPPED, and cleans up", async () => {
  const { readFile } = await import("node:fs/promises");
  const { asObject } = await import("../../tools/e2e/checks/shared.ts");
  const deps = dependencies("");
  let username = "";
  let applied = false;
  const ldif: string[] = [];
  const calls: string[] = [];
  const api = {
    async request(namespace: string, payload: JsonValue) {
      calls.push(namespace);
      if (namespace === "ClusterSettings.GetModuleSettings") return response({ settings: { provider: "activedirectory" } });
      if (namespace === "Accounts.CreateUser") {
        username = String(asObject(asObject(payload)?.user)?.username);
        return response({ user: { username, uid: 6000, gid: 6000 } });
      }
      if (namespace === "VirtualDesktopAdmin.CreateSession") return response({ session: { idea_session_id: "proof-session", owner: username } });
      if (namespace === "VirtualDesktopAdmin.GetSessionInfo") return response({ session: { state: applied ? "STOPPED" : "READY" } });
      if (namespace === "Accounts.ReconcileUsers") {
        if (asObject(payload)?.dry_run === false) applied = true;
        return response({ refused: 0, errors: 0, changes: [{ username, action: "disable" }] });
      }
      if (namespace === "Accounts.GetUser") return response({ user: { enabled: !applied } });
      if (namespace === "ClusterSettings.ListClusterModules") return response({ listing: [{ name: "virtual-desktop-controller", module_id: "vdc" }] });
      if (namespace === "Accounts.AddUserToGroup") return response({});
      if (namespace === "Projects.GetProject") return response({ project: { project_id: "proof-project", ldap_groups: ["proof-group"] } });
      if (namespace === "Projects.GetUserProjects") return response({ projects: [{ project_id: "proof-project" }] });
      if (namespace === "VirtualDesktopAdmin.DeleteSessions" || namespace === "Accounts.DeleteUser") return response({});
      throw new Error(`unexpected API ${namespace}`);
    },
  };
  const processes = {
    async run(command: string, args: string[]) {
      calls.push(command);
      const index = args.indexOf("-f");
      if (index >= 0) ldif.push(await readFile(args[index + 1]!, "utf8"));
      assert.ok(args.includes("-y"));
      return { exitCode: 0, stderr: "", stdout: "" };
    },
  };
  const options = parseProofMatrixOptions([
    "--check", "account-reconcile", "--ldap-uri", "ldaps://directory.example.invalid",
    "--ldap-bind-dn", "CN=bind,DC=example,DC=invalid", "--ldap-user-base", "OU=Users,DC=example,DC=invalid",
    "--ldap-password-file", "/tmp/directory-password", "--desktop-request", '{"session":{"project":{"project_id":"proof-project"}}}',
  ], {});
  const run = await runProofMatrix(options, { ...deps, api, processes });
  assert.equal(run.exitCode, 0);
  assert.equal(run.results[0]?.result.passed, true);
  assert.ok(ldif.some((text) => text.includes("userAccountControl: 514")));
  assert.deepEqual(calls.slice(-3), ["VirtualDesktopAdmin.DeleteSessions", "Accounts.DeleteUser", "ldapdelete"]);
});
