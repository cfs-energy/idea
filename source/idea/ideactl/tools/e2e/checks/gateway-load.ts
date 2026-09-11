import { failed, requiredOption } from "./shared.ts";
import type { ProofCheck } from "./types.ts";

/** Runs the existing gateway load tool and evaluates its final connection output. */
export const gatewayLoadCheck: ProofCheck = {
  name: "gateway-load",
  description: "Run gateway TLS load and compare handshakes and failures with thresholds.",
  requiredFlags: (options) => [
    ...(options.gatewayHost === undefined ? ["gateway-host"] : []),
    ...(options.gatewayMaxP95Ms === undefined ? ["gateway-max-p95-ms"] : []),
    ...(options.gatewayMaxFailures === undefined ? ["gateway-max-failures"] : []),
  ],
  async run(context) {
    context.output("ACTION run the gateway TLS load tool");
    const result = await context.processes.run(process.execPath, [
      "tools/e2e/load-gateway.ts",
      "--host",
      requiredOption(context.options, "gatewayHost"),
      "--port",
      String(context.options.gatewayPort ?? 443),
      "--connections",
      String(context.options.gatewayConnections ?? 5_000),
      "--ramp",
      String(context.options.gatewayRampSeconds ?? 60),
      "--hold",
      String(context.options.gatewayHoldSeconds ?? 120),
      ...(context.options.insecureTls ? ["--insecure"] : []),
    ]);
    const summary = gatewaySummary(result.stdout);
    const observed = [`exit=${result.exitCode}`, summary === undefined ? "no final gateway load summary" : summary.text, ...outputLines(result.stderr)];
    if (result.exitCode !== 0 || summary === undefined) {
      return failed(...observed);
    }
    const maxP95Ms = requiredOption(context.options, "gatewayMaxP95Ms");
    const maxFailures = requiredOption(context.options, "gatewayMaxFailures");
    const withinLimits = summary.p95Ms <= maxP95Ms && summary.failures <= maxFailures;
    return withinLimits
      ? { observed: [...observed, `p95_limit=${maxP95Ms}ms failure_limit=${maxFailures}`], passed: true }
      : failed(...observed, `p95_limit=${maxP95Ms}ms failure_limit=${maxFailures}`);
  },
};

/** Parses the final line emitted by the existing gateway load tool. */
export function gatewaySummary(stdout: string): { failures: number; p95Ms: number; text: string } | undefined {
  const line = lastLineStartingWith(stdout, "DONE opened=");
  if (line === undefined) {
    return undefined;
  }
  const p95 = /handshake_p50=\d+(?:\.\d+)?ms p95=(\d+(?:\.\d+)?)ms/.exec(line);
  const failures = /failed=(\{.*\}) handshake_p50=/.exec(line);
  if (p95?.[1] === undefined || failures?.[1] === undefined) {
    return undefined;
  }
  try {
    const parsed: unknown = JSON.parse(failures[1]);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return undefined;
    }
    const total = Object.values(parsed).reduce((sum, value) => sum + (typeof value === "number" ? value : 0), 0);
    return { failures: total, p95Ms: Number(p95[1]), text: line };
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

/** Keeps child error output concise in the check transcript. */
function outputLines(value: string): string[] {
  return value.trim() === "" ? [] : value.trim().split("\n").map((line) => `stderr=${line}`);
}
