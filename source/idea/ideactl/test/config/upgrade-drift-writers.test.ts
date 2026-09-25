import assert from "node:assert/strict";
import test from "node:test";

import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient, GetCommand, UpdateCommand } from "@aws-sdk/lib-dynamodb";
import { ClusterConfigDb } from "../../src/config/cluster-config-db.ts";

test("sync stamps every write as template and CLI edits replace the marker atomically", async (context) => {
  context.mock.method(DynamoDBClient.prototype, "send", async () => ({ Table: { TableStatus: "ACTIVE" } }));
  const rows = new Map<string, Record<string, unknown>>();
  context.mock.method(DynamoDBDocumentClient.prototype, "send", async (command: GetCommand | UpdateCommand) => {
    const key = String(command.input.Key?.["key"]);
    if (command instanceof GetCommand) return { Item: rows.get(key) };
    assert.equal(command.input.UpdateExpression, "SET #value=:value, #source=:source ADD #version :version");
    assert.equal(command.input.ExpressionAttributeNames?.["#source"], "source");
    const values = command.input.ExpressionAttributeValues!;
    rows.set(key, { key, value: values[":value"], source: values[":source"], version: Number(rows.get(key)?.["version"] ?? 0) + 1 });
    return {};
  });
  const client = new DynamoDBClient({ region: "us-east-1" });
  try {
    const db = await ClusterConfigDb.open({ clusterName: "sample", client, awsRegion: "us-east-1" });
    const entry = { key: "global-settings.default", value: ["old"] };
    await db.syncClusterSettingsInDb([entry]);
    assert.equal(rows.get(entry.key)?.["source"], "template");
    await db.setConfigEntry(entry.key, ["operator"]);
    assert.equal(rows.get(entry.key)?.["source"], "cli");
    await db.syncClusterSettingsInDb([entry]);
    assert.equal(rows.get(entry.key)?.["source"], "cli");
    assert.equal(rows.get(entry.key)?.["version"], 2);
    await db.syncClusterSettingsInDb([{ ...entry, value: ["new"] }], true);
    assert.deepEqual(rows.get(entry.key), { ...entry, value: ["new"], source: "template", version: 3 });
  } finally {
    client.destroy();
  }
});
