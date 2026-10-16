import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { runOracleProcess } from "../support/oracle-process.ts";

test("oracle drains large requests/responses without pipe deadlock", async () => {
  const input = "x".repeat(2 * 1024 * 1024);
  const result = await runOracleProcess(process.execPath, ["-e", 'process.stdout.write(require("node:fs").readFileSync(0))'], { input });
  assert.equal(result, input);
});

test("a failed spawn closes descriptors before removing its scratch directory", async () => {
  await assert.rejects(runOracleProcess("/nonexistent/ideactl-oracle", [], { input: "" }), { code: "ENOENT" });
});

test("a timed-out oracle is reaped and the next request succeeds", async () => {
  const scratch = mkdtempSync(join(tmpdir(), "oracle-timeout-"));
  try {
    const pidFile = join(scratch, "pid");
    await assert.rejects(runOracleProcess(process.execPath, ["-e", `require("node:fs").writeFileSync(${JSON.stringify(pidFile)}, String(process.pid)); setInterval(() => {}, 1000);`], { input: "", timeoutMs: 2000 }), /oracle timed out/);
    const pid = Number(readFileSync(pidFile, "utf8"));
    assert.throws(() => process.kill(pid, 0), { code: "ESRCH" });
    assert.equal(await runOracleProcess(process.execPath, ["-e", 'process.stdout.write("next")'], { input: "" }), "next");
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
});


test("Python watchdog exits when its parent is killed without cleanup", async () => {
  const oracle = fileURLToPath(new URL("./jinja-branch-oracle.py", import.meta.url));
  const program = `import runpy, time; runpy.run_path(${JSON.stringify(oracle)})['watch_parent'](); print('ready', flush=True); time.sleep(120)`;
  const parent = spawn(process.execPath, ["-e", `
    const child = require('node:child_process').spawn('python3', ['-c', ${JSON.stringify(program)}]);
    child.stdout.once('data', () => console.log(child.pid));
    child.stderr.pipe(process.stderr);
  `], { stdio: ["ignore", "pipe", "pipe"] });
  let pid: number | undefined;
  try {
    pid = await new Promise<number>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("oracle did not become ready")), 15_000);
      parent.once("error", reject);
      parent.stdout.once("data", (chunk: Buffer) => { clearTimeout(timer); resolve(Number(chunk.toString().trim())); });
      parent.once("exit", () => { clearTimeout(timer); reject(new Error("oracle parent exited before readiness")); });
    });
    assert.ok(Number.isInteger(pid) && pid > 0);
    parent.kill("SIGKILL");
    const deadline = Date.now() + 10_000;
    let alive = true;
    while (Date.now() < deadline) {
      try { process.kill(pid, 0); } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error;
        alive = false;
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    assert.equal(alive, false, "orphaned Python oracle is still running");
    assert.equal(await runOracleProcess(process.execPath, ["-e", 'process.stdout.write("recovered")'], { input: "" }), "recovered");
  } finally {
    parent.kill("SIGKILL");
    if (pid !== undefined) { try { process.kill(pid, "SIGKILL"); } catch { /* Already reaped. */ } }
  }
});
