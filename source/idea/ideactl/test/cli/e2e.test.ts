import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { endpointPath, IdeaApiClient, requestEnvelope } from "../../tools/e2e/api.ts";
import { latencyStats } from "../../tools/e2e/load-api.ts";

test("routes namespaces and constructs the API envelope", () => {
  assert.equal(endpointPath("Scheduler.ListActiveJobs"), "/scheduler/api/v1");
  assert.equal(endpointPath("VirtualDesktop.ListSessions"), "/vdc/api/v1");
  assert.equal(endpointPath("Accounts.GetUser"), "/cluster-manager/api/v1");
  assert.deepEqual(requestEnvelope("Projects.ListProjects", {}, "request-1"), {
    header: { namespace: "Projects.ListProjects", request_id: "request-1" },
    payload: {},
  });
});

test("refreshes an unauthorized token and retries the original request", async () => {
  const directory = mkdtempSync(join(tmpdir(), "ideactl-e2e-"));
  const passwordFile = join(directory, "password.txt");
  const tokenDirectory = join(directory, "tokens");
  writeFileSync(passwordFile, "password-value\n", "utf8");

  const responses = [
    { payload: { auth: { access_token: "first-token" } } },
    { error_code: "TOKEN_EXPIRED" },
    { payload: { auth: { access_token: "second-token" } } },
    { success: true, payload: { listing: [] } },
  ];
  const calls: Array<{ body: string; headers: RequestInit["headers"] | undefined; url: string }> = [];
  const fetchStub: typeof fetch = async (input, init) => {
    calls.push({
      body: typeof init?.body === "string" ? init.body : "",
      headers: init?.headers,
      url: String(input),
    });
    const response = responses.shift();
    if (response === undefined) {
      throw new Error("unexpected fetch call");
    }
    return new Response(JSON.stringify(response), { status: 200 });
  };

  try {
    const client = new IdeaApiClient({
      albHost: "control-plane.example.net",
      username: "test-user",
      passwordFile,
      tokenDirectory,
      fetchImpl: fetchStub,
    });
    const result = await client.request("Scheduler.ListActiveJobs", {});

    assert.equal(result.status, 200);
    assert.equal(calls.length, 4);
    assert.equal(calls[0]?.url, "https://control-plane.example.net/cluster-manager/api/v1");
    assert.match(calls[0]?.body ?? "", /"namespace":"Auth\.InitiateAuth"/);
    assert.equal(calls[1]?.url, "https://control-plane.example.net/scheduler/api/v1");
    assert.match(calls[1]?.body ?? "", /"namespace":"Scheduler\.ListActiveJobs"/);
    assert.deepEqual(calls[1]?.headers, {
      Authorization: "Bearer first-token",
      "Content-Type": "application/json",
    });
    assert.deepEqual(calls[3]?.headers, {
      Authorization: "Bearer second-token",
      "Content-Type": "application/json",
    });
    assert.equal(readFileSync(join(tokenDirectory, "test-user.token"), "utf8"), "second-token\n");
  } finally {
    rmSync(directory, { force: true, recursive: true });
  }
});

test("reduces known latency samples to p50, p95, and p99", () => {
  assert.deepEqual(latencyStats([40, 10, 30, 20]), {
    count: 4,
    maxMs: 40,
    p50Ms: 25,
    p95Ms: 40,
    p99Ms: 40,
  });

  // 101 samples so nearest-rank p95 and p99 sit below the max.
  const samples = Array.from({ length: 101 }, (_, index) => (index + 1) * 10);
  assert.deepEqual(latencyStats(samples), {
    count: 101,
    maxMs: 1010,
    p50Ms: 510,
    p95Ms: 960,
    p99Ms: 1000,
  });
});
