import { spawn } from "node:child_process";
import { closeSync, mkdtempSync, openSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

/** Regular files avoid pipe backpressure and give the oracle an unambiguous EOF. */
export async function runOracleProcess(
  command: string,
  args: string[],
  options: { input: string; env?: NodeJS.ProcessEnv; timeoutMs?: number },
): Promise<string> {
  const scratch = mkdtempSync(join(tmpdir(), "ideactl-oracle-"));
  const descriptors: number[] = [];
  try {
    writeFileSync(join(scratch, "input"), options.input, { mode: 0o600 });
    descriptors.push(openSync(join(scratch, "input"), "r"));
    descriptors.push(openSync(join(scratch, "output"), "w", 0o600));
    descriptors.push(openSync(join(scratch, "error"), "w", 0o600));
    const child = spawn(command, args, { env: options.env, stdio: descriptors });
    const kill = (): void => { child.kill("SIGKILL"); };
    const interrupt = (): void => { kill(); process.exit(130); };
    process.once("exit", kill);
    process.once("SIGINT", interrupt);
    process.once("SIGTERM", interrupt);
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; kill(); }, options.timeoutMs ?? 60_000);
    try {
      await new Promise<void>((resolve, reject) => {
        let launchError: Error | undefined;
        child.once("error", (error) => { launchError = error; });
        // close, rather than exit, also waits for the child's descriptors to close.
        child.once("close", (code, signal) => {
          if (launchError !== undefined) reject(launchError);
          else if (code === 0 && !timedOut) resolve();
          else reject(new Error(`${command}: ${timedOut ? "oracle timed out" : `exit ${code ?? signal}`}\n${readFileSync(join(scratch, "error"), "utf8")}`));
        });
      });
      return readFileSync(join(scratch, "output"), "utf8");
    } finally {
      clearTimeout(timer);
      process.removeListener("exit", kill);
      process.removeListener("SIGINT", interrupt);
      process.removeListener("SIGTERM", interrupt);
    }
  } finally {
    for (const descriptor of descriptors) closeSync(descriptor);
    rmSync(scratch, { recursive: true, force: true });
  }
}
