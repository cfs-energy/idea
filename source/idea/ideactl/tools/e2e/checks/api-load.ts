import { failed, requiredOption } from "./shared.ts";
import type { ProofCheck } from "./types.ts";

/** Runs the existing API load tool and evaluates its final latency and error output. */
export const apiLoadCheck: ProofCheck = {
  name: "api-load",
  description: "Run the portal API load mix and compare its final values with thresholds.",
  requiredFlags: (options) => [
    ...(options.albHost === undefined ? ["alb-host"] : []),
    ...(options.username === undefined ? ["username"] : []),
    ...(options.passwordFile === undefined ? ["password-file"] : []),
    ...(options.apiMaxP95Ms === undefined ? ["api-max-p95-ms"] : []),
    ...(options.apiMaxErrorCount === undefined ? ["api-max-error-count"] : []),
  ],
  async run(context) {
    context.output("ACTION run the API load tool");
    const result = await context.processes.run(process.execPath, [
      "tools/e2e/load-api.ts",
      "--alb-host",
      requiredOption(context.options, "albHost"),
      "--username",
      requiredOption(context.options, "username"),
      "--password-file",
      requiredOption(context.options, "passwordFile"),
      "--rps",
      String(context.options.apiRps ?? 50),
      "--seconds",
      String(context.options.apiSeconds ?? 300),
      "--workers",
      String(context.options.apiWorkers ?? 64),
      ...tokenArguments(context.options.tokenDirectory),
      ...(context.options.insecureTls ? ["--insecure"] : []),
    ]);
    const summary = apiSummary(result.stdout);
    const observed = [
      `exit=${result.exitCode}`,
      summary === undefined ? "no final API load summary" : `p95=${summary.p95Ms}ms errors=${summary.errors}`,
      ...outputLines(result.stderr),
    ];
    if (result.exitCode !== 0 || summary === undefined) {
      return failed(...observed);
    }
    const maxP95Ms = requiredOption(context.options, "apiMaxP95Ms");
    const maxErrors = requiredOption(context.options, "apiMaxErrorCount");
    const withinLimits = summary.p95Ms <= maxP95Ms && summary.errors <= maxErrors;
    return withinLimits
      ? { observed: [...observed, `p95_limit=${maxP95Ms}ms error_limit=${maxErrors}`], passed: true }
      : failed(...observed, `p95_limit=${maxP95Ms}ms error_limit=${maxErrors}`);
  },
};

/** Parses the final line emitted by the existing API load tool. */
export function apiSummary(stdout: string): { errors: number; p95Ms: number } | undefined {
  const line = lastLineStartingWith(stdout, "DONE requests=");
  if (line === undefined) {
    return undefined;
  }
  const p95 = /p95=(\d+(?:\.\d+)?)ms/.exec(line);
  const errors = /errors=(\{.*\})$/.exec(line);
  if (p95?.[1] === undefined || errors?.[1] === undefined) {
    return undefined;
  }
  try {
    const parsed: unknown = JSON.parse(errors[1]);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return undefined;
    }
    const total = Object.values(parsed).reduce((sum, value) => sum + (typeof value === "number" ? value : 0), 0);
    return { errors: total, p95Ms: Number(p95[1]) };
  } catch {
    return undefined;
  }
}

/** Finds the final summary line without requiring a newer JavaScript library target. */
function lastLineStartingWith(value: string, prefix: string): string | undefined {
  const lines = value.split("\n");
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const line = lines[index];
    if (line?.startsWith(prefix)) {
      return line;
    }
  }
  return undefined;
}

/** Produces an optional argument pair without passing undefined to the child process. */
function tokenArguments(tokenDirectory: string | undefined): string[] {
  return tokenDirectory === undefined ? [] : ["--token-dir", tokenDirectory];
}

/** Keeps child error output concise in the check transcript. */
function outputLines(value: string): string[] {
  return value.trim() === "" ? [] : value.trim().split("\n").map((line) => `stderr=${line}`);
}
