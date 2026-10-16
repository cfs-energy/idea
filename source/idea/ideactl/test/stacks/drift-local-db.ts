/**
 * Start the local DynamoDB emulator with a real Java runtime.
 *
 * The upgrade merge rules depend on GetItem existence, typed attributes, and
 * prefix deletes. An in-process fake is not a substitute.
 */

import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";

import { DynamoDBClient, ListTablesCommand } from "@aws-sdk/client-dynamodb";

import { requiredService } from "../support/fixtures.ts";

const JAR = path.join(os.homedir(), ".idea/lib/dynamodb-local/DynamoDBLocal.jar");

export interface LocalEmulator {
  endpoint: string;
  how: string;
  java: string;
  stop: () => void;
}

/** Return a free TCP port on loopback. */
async function freePort(): Promise<number> {
  return await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (address === null || typeof address === "string") {
        server.close();
        reject(new Error("failed to allocate a loopback port"));
        return;
      }
      const port = address.port;
      server.close(() => resolve(port));
    });
  });
}

/** Wait for a successful DynamoDB operation, including during a slow JVM startup. */
export async function waitForDynamoDb(
  endpoint: string,
  timeoutMs = 90_000,
  startupFailure: () => string | undefined = () => undefined,
): Promise<void> {
  const client = new DynamoDBClient({
    endpoint,
    region: "us-east-2",
    credentials: { accessKeyId: "local", secretAccessKey: "local" },
    maxAttempts: 1,
    requestHandler: { connectionTimeout: 1000, requestTimeout: 2000 },
  });
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown;
  try {
    while (Date.now() < deadline) {
      const failure = startupFailure();
      if (failure !== undefined) throw new Error(failure);
      try {
        await client.send(new ListTablesCommand({ Limit: 1 }), {
          abortSignal: AbortSignal.timeout(Math.max(1, Math.min(2000, deadline - Date.now()))),
        });
        return;
      } catch (error) {
        lastError = error;
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    throw new Error(`DynamoDB readiness exceeded ${timeoutMs}ms at ${endpoint}: ${String(lastError)}`);
  } finally {
    client.destroy();
  }
}

/**
 * Resolve a Java binary that can actually launch. The macOS `/usr/bin/java`
 * stub is not enough; Homebrew OpenJDK is.
 */
export function resolveJava(): string {
  const fromHome = process.env["JAVA_HOME"];
  const candidates = [
    fromHome === undefined || fromHome === "" ? undefined : path.join(fromHome, "bin", "java"),
    "/opt/homebrew/opt/openjdk@26/bin/java",
    "/opt/homebrew/opt/openjdk/bin/java",
    "java",
  ];
  for (const candidate of candidates) {
    if (candidate === undefined) continue;
    if (candidate !== "java" && !fs.existsSync(candidate)) continue;
    if (spawnSync(candidate, ["-version"], { stdio: "ignore" }).status === 0) return candidate;
  }
  return requiredService(
    "local database emulator",
    `${JAR} needs a Java runtime. Install OpenJDK and retry, or set JAVA_HOME.`,
  );
}

/** Start DynamoDB Local on a free port, or attach to DDB_LOCAL_ENDPOINT. */
export async function startDynamoDbLocal(): Promise<LocalEmulator> {
  const existing = process.env["DDB_LOCAL_ENDPOINT"];
  if (existing !== undefined && existing !== "") {
    await waitForDynamoDb(existing);
    return {
      endpoint: existing,
      how: "DDB_LOCAL_ENDPOINT",
      java: "external",
      stop: () => {},
    };
  }

  if (!fs.existsSync(JAR)) {
    return requiredService(
      "local database emulator",
      `java -jar ${JAR} -inMemory -port 8000`,
    );
  }

  const java = resolveJava();
  const port = await freePort();
  const child: ChildProcess = spawn(
    java,
    [
      `-Djava.library.path=${path.join(path.dirname(JAR), "DynamoDBLocal_lib")}`,
      "-jar",
      JAR,
      "-inMemory",
      "-port",
      String(port),
    ],
    { stdio: ["ignore", "pipe", "pipe"], cwd: path.dirname(JAR) },
  );
  let output = "";
  let launchError: Error | undefined;
  child.on("error", (error) => { launchError = error; });
  const capture = (chunk: Buffer): void => { output = (output + chunk.toString()).slice(-8192); };
  child.stdout?.on("data", capture);
  child.stderr?.on("data", capture);
  const stop = (): void => { child.kill("SIGKILL"); };
  process.once("exit", stop);
  child.once("close", () => process.removeListener("exit", stop));
  try {
    await waitForDynamoDb(`http://127.0.0.1:${port}`, 90_000, () => {
      if (launchError !== undefined) return launchError.message;
      if (child.exitCode !== null || child.signalCode !== null) {
        return `DynamoDB exited (${child.exitCode ?? child.signalCode}): ${output}`;
      }
      return undefined;
    });
  } catch (error) {
    stop();
    throw new Error(`${String(error)}\nDynamoDB startup output: ${output}`);
  }
  return {
    endpoint: `http://127.0.0.1:${port}`,
    how: `${java} -jar ${JAR}`,
    java,
    stop,
  };
}
