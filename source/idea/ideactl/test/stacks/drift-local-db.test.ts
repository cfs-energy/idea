import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";
import { waitForDynamoDb } from "./drift-local-db.ts";

test("DynamoDB readiness retries service failures instead of accepting any HTTP response", async (t) => {
  let requests = 0;
  const server = http.createServer((req, res) => {
    assert.equal(req.headers["x-amz-target"], "DynamoDB_20120810.ListTables");
    req.resume();
    requests += 1;
    res.writeHead(requests < 3 ? 503 : 200, { "Content-Type": "application/x-amz-json-1.0" });
    res.end(requests < 3 ? '{"__type":"ServiceUnavailable"}' : '{"TableNames":[]}');
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  const address = server.address();
  assert.ok(address !== null && typeof address !== "string");
  await waitForDynamoDb(`http://127.0.0.1:${address.port}`, 10_000);
  assert.equal(requests, 3);
  await assert.rejects(waitForDynamoDb(`http://127.0.0.1:${address.port}`, 1000, () => "JVM exited: bind failed"), /JVM exited: bind failed/);
});
