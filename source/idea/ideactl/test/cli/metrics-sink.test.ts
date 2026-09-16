import assert from "node:assert/strict";
import test from "node:test";
import { createLiveDependencies, missingRequiredFlags, parseProofMatrixOptions, runProofMatrix, usage } from "../../tools/e2e/proof-matrix.ts";
import type { ProofMatrixOptions } from "../../tools/e2e/checks/types.ts";

const now = Date.parse("2026-09-15T12:00:00Z");
const options = parseProofMatrixOptions([
  "--check", "metrics-sink", "--cluster", "sample-cluster",
  "--datadog-api-key", "test-api-key", "--datadog-app-key", "test-app-key",
], {});

function replay(body: unknown, status = 200) {
  const lines: string[] = [];
  const requests: Array<{ url: URL; headers: Headers }> = [];
  return {
    ...createLiveDependencies(options),
    lines,
    requests,
    now: () => now,
    output: (line: string) => { lines.push(line); },
    fetch: (async (input, init) => {
      requests.push({ url: new URL(String(input)), headers: new Headers(init?.headers) });
      return new Response(JSON.stringify(body), { status });
    }) as typeof fetch,
  };
}

test("points pass with a count, newest timestamp, cluster filter and 15-minute window", async () => {
  const deps = replay({ status: "ok", series: [
    { pointlist: [[now - 120_000, 2], [now - 60_000, 0], [now, null]] },
    { pointlist: [[now - 180_000, 3]] },
  ] });
  const result = await runProofMatrix(options, deps);
  assert.equal(result.exitCode, 0);
  assert.ok(deps.lines.some((line) => line.startsWith("CHECK metrics-sink:")));
  assert.ok(deps.lines.some((line) => line.startsWith("ACTION query Datadog")));
  assert.ok(deps.lines.includes("OBSERVED metrics-sink: points=3 newest=2026-09-15T11:59:00.000Z"));
  assert.ok(deps.lines.includes("PASS metrics-sink"));
  assert.equal(deps.requests.length, 1);
  const { url, headers } = deps.requests[0];
  assert.equal(url.origin, "https://api.datadoghq.com");
  assert.equal(url.pathname, "/api/v1/query");
  assert.equal(url.searchParams.get("query"), "sum:idea.api_invocations{idea_cluster:sample-cluster}");
  assert.equal(url.searchParams.get("from"), String(now / 1000 - 900));
  assert.equal(url.searchParams.get("to"), String(now / 1000));
  assert.equal(headers.get("DD-API-KEY"), "test-api-key");
  assert.equal(headers.get("DD-APPLICATION-KEY"), "test-app-key");
  assert.ok(!deps.lines.join("\n").includes("test-api-key"));
  assert.ok(!deps.lines.join("\n").includes("test-app-key"));
});

for (const series of [[], [{ pointlist: [] }], [{ pointlist: [[now, null]] }]]) {
  test(`no measured points fails: ${JSON.stringify(series)}`, async () => {
    const deps = replay({ status: "ok", series });
    assert.equal((await runProofMatrix(options, deps)).exitCode, 1);
    assert.ok(deps.lines.includes("OBSERVED metrics-sink: points=0 newest=none"));
    assert.ok(deps.lines.includes("FAIL metrics-sink"));
  });
}

for (const keys of [{}, { datadogApiKey: "key" }, { datadogAppKey: "key" }]) {
  test(`missing keys skips without a request: ${Object.keys(keys).join(",") || "both"}`, async () => {
    const skipped: ProofMatrixOptions = { checks: ["metrics-sink"], insecureTls: false, ...keys };
    assert.deepEqual(missingRequiredFlags(skipped), []);
    const deps = replay({});
    const run = await runProofMatrix(skipped, deps);
    assert.equal(run.exitCode, 0);
    assert.equal(run.results[0].result.skipped, true);
    assert.ok(deps.lines.includes("NOT RUN metrics-sink"));
    assert.deepEqual(deps.requests, []);
    assert.doesNotThrow(() => createLiveDependencies(skipped));
  });
}

test("flags, environment, default selection and help expose the metrics check", async () => {
  assert.equal(options.datadogSite, "datadoghq.com");
  assert.ok(parseProofMatrixOptions([], {}).checks.includes("metrics-sink"));
  const custom = parseProofMatrixOptions(["--check", "metrics-sink", "--datadog-site", "datadoghq.eu"], {
    IDEA_E2E_CLUSTER: "sample-cluster",
    IDEA_E2E_DATADOG_API_KEY: "key",
    IDEA_E2E_DATADOG_APP_KEY: "app",
    IDEA_E2E_DATADOG_SITE: "us5.datadoghq.com",
  });
  assert.deepEqual(missingRequiredFlags(custom), []);
  assert.deepEqual(missingRequiredFlags({ ...custom, cluster: undefined }), ["cluster"]);
  assert.equal(parseProofMatrixOptions([], { IDEA_E2E_DATADOG_SITE: "us5.datadoghq.com" }).datadogSite, "us5.datadoghq.com");
  const deps = replay({ status: "ok", series: [] });
  await runProofMatrix(custom, deps);
  assert.equal(deps.requests[0].url.hostname, "api.datadoghq.eu");
  for (const flag of ["--datadog-api-key", "--datadog-app-key", "--datadog-site"]) assert.ok(usage().includes(flag));
  assert.ok(usage().includes("metrics-sink"));
});

test("HTTP and API errors fail the matrix", async () => {
  for (const [body, status] of [[{}, 403], [{ status: "error" }, 200]] as const) {
    const deps = replay(body, status);
    assert.equal((await runProofMatrix(options, deps)).exitCode, 1);
    assert.ok(deps.lines.includes("FAIL metrics-sink"));
  }
});

test("transport errors fail the matrix", async () => {
  const deps = replay({});
  deps.fetch = async () => { throw new Error("request timed out"); };
  assert.equal((await runProofMatrix(options, deps)).exitCode, 1);
  assert.ok(deps.lines.includes("OBSERVED metrics-sink: request timed out"));
});
