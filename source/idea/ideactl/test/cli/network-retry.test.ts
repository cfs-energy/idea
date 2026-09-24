/**
 * A client network that drops new connections for a few seconds must cost the upgrade a pause,
 * not an abort: AWS calls and CDK runs retry transient network failures with growing backoff.
 */
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";

import { DynamoDBClient, ListTablesCommand } from "@aws-sdk/client-dynamodb";

import { AWS_CALL_ATTEMPTS, networkTolerantRetryStrategy, retryDelayMs } from "../../src/cli/aws-client-options.ts";
import { CDK_RUN_ATTEMPTS, CdkInvoker, type Deps } from "../../src/cli/cdk-invoker.ts";

let home = "";
const previousHome = process.env.IDEA_USER_HOME;
before(() => {
  home = mkdtempSync(join(tmpdir(), "ideactl-network-retry-"));
  process.env.IDEA_USER_HOME = home;
});
after(() => {
  if (previousHome === undefined) delete process.env.IDEA_USER_HOME;
  else process.env.IDEA_USER_HOME = previousHome;
  rmSync(home, { recursive: true, force: true });
});

test("backoff grows 1, 2, 4, 8, 16 s and then holds at 20 s", () => {
  assert.deepEqual([1, 2, 3, 4, 5, 6, 9].map(retryDelayMs), [1000, 2000, 4000, 8000, 16000, 20000, 20000]);
});

test("transient failures retry up to the attempt budget; client errors fail at once", async () => {
  const s = networkTolerantRetryStrategy;
  let token = await s.acquireInitialRetryToken("scope");
  for (let retry = 1; retry < AWS_CALL_ATTEMPTS; retry++) {
    token = await s.refreshRetryTokenForRetry(token, { errorType: "TRANSIENT" });
    assert.equal(token.getRetryDelay(), retryDelayMs(retry));
  }
  await assert.rejects(s.refreshRetryTokenForRetry(token, { errorType: "TRANSIENT" }));
  await assert.rejects(s.refreshRetryTokenForRetry(await s.acquireInitialRetryToken("scope"), { errorType: "CLIENT_ERROR" }));
});

function flakyClient(failures: number, withStrategy: boolean) {
  let calls = 0;
  const client = new DynamoDBClient({
    region: "us-east-2",
    credentials: { accessKeyId: "AKIDEXAMPLE", secretAccessKey: "secret" },
    ...(withStrategy ? { retryStrategy: networkTolerantRetryStrategy } : {}),
    requestHandler: {
      handle: async () => {
        calls++;
        if (calls <= failures) throw Object.assign(new Error("connect ENETUNREACH"), { code: "ENETUNREACH" });
        return {
          response: {
            statusCode: 200,
            headers: { "content-type": "application/x-amz-json-1.0" },
            body: new Response(JSON.stringify({ TableNames: ["t"] })).body,
          },
        };
      },
    } as never,
  });
  return { client, calls: () => calls };
}

test("a real SDK client rides out four unreachable-network failures that the SDK default does not", async () => {
  // The default gives up after three attempts, so the same outage fails without this strategy.
  const plain = flakyClient(4, false);
  await assert.rejects(plain.client.send(new ListTablesCommand({})), /ENETUNREACH/);
  const tolerant = flakyClient(4, true);
  const result = await tolerant.client.send(new ListTablesCommand({}));
  assert.deepEqual(result.TableNames, ["t"]);
  assert.equal(tolerant.calls(), 5);
});

function invoker(runs: Array<{ code: number; stderr: string }>, waits: number[]) {
  let index = 0;
  const deps = {
    uuid: () => "deployment",
    out: () => {},
    sleep: async (ms: number) => { waits.push(ms); },
    spawn: async (_argv: string[], options: { onStderr?: (chunk: string) => void }) => {
      const run = runs[Math.min(index++, runs.length - 1)]!;
      options.onStderr?.(run.stderr);
      return run.code;
    },
  } as unknown as Deps;
  return {
    cdk: new CdkInvoker({ clusterName: "c", awsRegion: "us-east-2", moduleId: "m", moduleName: "m", moduleSet: "default", deps }),
    runs: () => index,
  };
}

test("a CDK run that fails on a network error is run again after a backoff", async () => {
  const waits: number[] = [];
  const { cdk, runs } = invoker([{ code: 1, stderr: "Failed to publish asset: read EADDRNOTAVAIL" }, { code: 0, stderr: "" }], waits);
  await cdk.execCdk(["cdk", "deploy"]);
  assert.equal(runs(), 2);
  assert.deepEqual(waits, [1000]);
});

test("a CDK run that fails for any other reason stops at once", async () => {
  const waits: number[] = [];
  const { cdk, runs } = invoker([{ code: 1, stderr: "ValidationError: Template format error" }], waits);
  await assert.rejects(cdk.execCdk(["cdk", "deploy"]));
  assert.equal(runs(), 1);
  assert.deepEqual(waits, []);
});

test("a CDK network failure that never clears stops after the run budget", async () => {
  const waits: number[] = [];
  const { cdk, runs } = invoker([{ code: 1, stderr: "getaddrinfo ENOTFOUND cloudformation.us-east-2.amazonaws.com" }], waits);
  await assert.rejects(cdk.execCdk(["cdk", "deploy"]));
  assert.equal(runs(), CDK_RUN_ATTEMPTS);
  assert.equal(waits.length, CDK_RUN_ATTEMPTS - 1);
});
