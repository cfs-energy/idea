#!/usr/bin/env node

import { connect, type TLSSocket } from "node:tls";
import { latencyStats } from "./load-api.ts";

interface GatewayOptions {
  connections: number;
  holdSeconds: number;
  host: string;
  insecureTls: boolean;
  port: number;
  rampSeconds: number;
}

/** Opens and holds the configured number of TLS connections to the DCV gateway. */
async function runGatewayLoad(options: GatewayOptions): Promise<void> {
  const handshakesMs: number[] = [];
  const failures = new Map<string, number>();
  const liveSockets: TLSSocket[] = [];
  let opened = 0;

  const connections = Array.from({ length: options.connections }, (_, index) => {
    const delayMs = (options.rampSeconds * 1_000 * index) / options.connections;
    return sleep(delayMs).then(async () => {
      const result = await openConnection(options);
      if (result.socket === undefined) {
        increment(failures, result.failure ?? "Error");
        return;
      }
      handshakesMs.push(result.elapsedMs);
      liveSockets.push(result.socket);
      opened += 1;
    });
  });

  const untilMs = (options.rampSeconds + options.holdSeconds) * 1_000;
  const startedAt = performance.now();
  while (performance.now() - startedAt < untilMs) {
    await sleep(15_000);
    const stats = latencyStats(handshakesMs);
    console.log(
      [
        `${clock()} open=${opened} failed=${JSON.stringify(Object.fromEntries(failures))}`,
        `handshake_p95=${stats.p95Ms.toFixed(0)}ms`,
      ].join(" "),
    );
  }

  for (const socket of liveSockets) {
    socket.destroy();
  }
  await Promise.all(connections);

  const stats = latencyStats(handshakesMs);
  if (stats.count === 0) {
    console.log("DONE nothing opened");
    return;
  }
  console.log(
    [
      `DONE opened=${opened} failed=${JSON.stringify(Object.fromEntries(failures))}`,
      `handshake_p50=${stats.p50Ms.toFixed(0)}ms p95=${stats.p95Ms.toFixed(0)}ms max=${stats.maxMs.toFixed(0)}ms`,
    ].join(" "),
  );
}

async function openConnection(options: GatewayOptions): Promise<{ elapsedMs: number; failure?: string; socket?: TLSSocket }> {
  const startedAt = performance.now();
  return new Promise((resolve) => {
    let settled = false;
    const socket = connect({
      host: options.host,
      port: options.port,
      rejectUnauthorized: !options.insecureTls,
      servername: options.host,
    });
    const timeout = setTimeout(() => finish("Timeout"), 20_000);

    function finish(failure?: string): void {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timeout);
      if (failure === undefined) {
        resolve({ elapsedMs: performance.now() - startedAt, socket });
        return;
      }
      socket.destroy();
      resolve({ elapsedMs: performance.now() - startedAt, failure });
    }

    socket.once("secureConnect", () => {
      socket.write(
        [
          "GET / HTTP/1.1",
          `Host: ${options.host}`,
          "Connection: keep-alive",
          "",
          "",
        ].join("\r\n"),
      );
    });
    socket.once("data", () => finish());
    socket.once("error", (error: Error) => finish(error.constructor.name));
  });
}

function increment(counts: Map<string, number>, key: string): void {
  counts.set(key, (counts.get(key) ?? 0) + 1);
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function clock(): string {
  return new Date().toISOString().slice(11, 19);
}

function usage(): string {
  return [
    "Usage: load-gateway.ts --host <nlb-host> [options]",
    "",
    "Options:",
    "  --port <number>        TLS port (default: 443)",
    "  --connections <count> Connections to hold (default: 5000)",
    "  --hold <seconds>       Hold time after ramp (default: 120)",
    "  --ramp <seconds>       Seconds to open all connections (default: 60)",
    "  --insecure             Disable TLS certificate verification",
  ].join("\n");
}

function parseOptions(argv: string[]): GatewayOptions {
  const flags = parseFlags(argv);
  if (flags.get("help") === true) {
    console.log(usage());
    process.exit(0);
  }
  return {
    connections: positiveInteger(flags, "connections", 5_000),
    holdSeconds: positiveNumber(flags, "hold", 120),
    host: requiredString(flags, "host"),
    insecureTls: flags.get("insecure") === true,
    port: positiveInteger(flags, "port", 443),
    rampSeconds: positiveNumber(flags, "ramp", 60),
  };
}

function parseFlags(argv: string[]): Map<string, string | boolean> {
  const flags = new Map<string, string | boolean>();
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (!argument.startsWith("--")) {
      throw new Error(`unexpected argument: ${argument}`);
    }
    const name = argument.slice(2);
    if (name === "help" || name === "insecure") {
      flags.set(name, true);
      continue;
    }
    const value = argv[index + 1];
    if (value === undefined || value.startsWith("--")) {
      throw new Error(`missing value for --${name}`);
    }
    flags.set(name, value);
    index += 1;
  }
  return flags;
}

function requiredString(flags: Map<string, string | boolean>, name: string): string {
  const value = optionalString(flags, name);
  if (value === undefined) {
    throw new Error(`missing required --${name}`);
  }
  return value;
}

function optionalString(flags: Map<string, string | boolean>, name: string): string | undefined {
  const value = flags.get(name);
  return typeof value === "string" && value !== "" ? value : undefined;
}

function positiveNumber(flags: Map<string, string | boolean>, name: string, fallback: number): number {
  const value = optionalString(flags, name);
  if (value === undefined) {
    return fallback;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`--${name} must be a positive number`);
  }
  return parsed;
}

function positiveInteger(flags: Map<string, string | boolean>, name: string, fallback: number): number {
  const value = positiveNumber(flags, name, fallback);
  if (!Number.isInteger(value)) {
    throw new Error(`--${name} must be an integer`);
  }
  return value;
}

if (import.meta.main) {
  try {
    await runGatewayLoad(parseOptions(process.argv.slice(2)));
  } catch (error: unknown) {
    console.error(error instanceof Error ? error.message : String(error));
    console.error(usage());
    process.exitCode = 2;
  }
}
