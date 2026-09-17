/**
 * Start the local DynamoDB emulator with a real Java runtime.
 *
 * The upgrade merge rules depend on GetItem existence, typed attributes, and
 * prefix deletes. An in-process fake is not a substitute.
 */

import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";

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

/** Wait until the emulator accepts HTTP, not merely TCP. */
async function waitForPort(port: number, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const ok = await new Promise<boolean>((resolve) => {
      const req = http.request(
        { host: "127.0.0.1", port, method: "GET", path: "/", timeout: 2000 },
        (res) => {
          res.resume();
          resolve(true);
        },
      );
      req.on("error", () => resolve(false));
      req.on("timeout", () => {
        req.destroy();
        resolve(false);
      });
      req.end();
    });
    if (ok) return true;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
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
    return {
      endpoint: existing,
      how: "DDB_LOCAL_ENDPOINT",
      java: resolveJava(),
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
    { stdio: "ignore", cwd: path.dirname(JAR) },
  );
  if (!(await waitForPort(port, 30_000))) {
    child.kill("SIGKILL");
    return requiredService(
      "local database emulator",
      `${java} -jar ${JAR} -inMemory -port ${port}`,
    );
  }
  return {
    endpoint: `http://127.0.0.1:${port}`,
    how: `${java} -jar ${JAR}`,
    java,
    stop: () => {
      child.kill("SIGKILL");
    },
  };
}
