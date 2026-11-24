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
    // A template overwrite keeps the operator's row: value, marker and version untouched.
    await db.syncClusterSettingsInDb([{ ...entry, value: ["new"] }], true);
    assert.deepEqual(rows.get(entry.key), { ...entry, value: ["operator"], source: "cli", version: 2 });
    // The operator's own overwrite still writes.
    await db.syncClusterSettingsInDb([{ ...entry, value: ["new"] }], true, "cli");
    assert.deepEqual(rows.get(entry.key), { ...entry, value: ["new"], source: "cli", version: 3 });
  } finally {
    client.destroy();
  }
});

test("the upgrade log shows a map or list value as written, not [object Object]", async (context) => {
  context.mock.method(DynamoDBClient.prototype, "send", async () => ({ Table: { TableStatus: "ACTIVE" } }));
  const owners = { scheduler: "upgrade-1", "cluster-manager": "upgrade-1" };
  context.mock.method(DynamoDBDocumentClient.prototype, "send", async (command: { constructor: { name: string } }) =>
    command.constructor.name === "ScanCommand"
      ? { Items: [{ key: "cluster.upgrade_module_set_owners", value: owners }, { key: "cluster.upgrade_hosts", value: ["a", "b"] }] }
      : {},
  );
  const lines: string[] = [];
  const db = await ClusterConfigDb.open({
    clusterName: "sample",
    client: new DynamoDBClient({ region: "us-east-1" }),
    awsRegion: "us-east-1",
    logger: (line) => lines.push(line),
  });
  await db.setConfigEntry("cluster.upgrade_module_set_owners", owners);
  await db.setConfigEntry("cluster.locale", "en_US.UTF-8");
  await db.deleteConfigEntries("cluster.upgrade_");
  const shown = JSON.stringify(owners);
  assert.ok(lines.includes(`updating config: cluster.upgrade_module_set_owners = ${shown}`), lines.join("\n"));
  assert.ok(lines.includes("updating config: cluster.locale = en_US.UTF-8"));
  assert.ok(lines.includes(`deleting config entry - cluster.upgrade_module_set_owners = ${shown}`));
  assert.ok(lines.includes('deleting config entry - cluster.upgrade_hosts = ["a","b"]'));
  assert.ok(lines.every((line) => !line.includes("[object Object]")));
});

test("a refused settings write says the credentials only read settings and where to run it", async (context) => {
  context.mock.method(DynamoDBClient.prototype, "send", async () => ({ Table: { TableStatus: "ACTIVE" } }));
  context.mock.method(DynamoDBDocumentClient.prototype, "send", async (command: { constructor: { name: string } }) => {
    if (command.constructor.name === "ScanCommand") return { Items: [{ key: "cluster.upgrade_hosts", value: "a" }] };
    throw Object.assign(
      new Error(
        "User: arn:aws:sts::000000000000:assumed-role/sample-vdc-controller-task-role/x is not authorized to perform: " +
          "dynamodb:UpdateItem on resource: arn:aws:dynamodb:us-east-1:000000000000:table/sample.cluster-settings",
      ),
      { name: "AccessDeniedException" },
    );
  });
  const db = await ClusterConfigDb.open({
    clusterName: "sample",
    client: new DynamoDBClient({ region: "us-east-1" }),
    awsRegion: "us-east-1",
  });
  const plain = (error: Error) =>
    error.message.startsWith("These AWS credentials cannot change sample.cluster-settings") &&
    error.message.includes("--aws-profile") &&
    !error.message.includes("arn:aws");
  await assert.rejects(() => db.setConfigEntry("cluster.locale", "en_US.UTF-8"), plain);
  // the AWS error is kept for IDEA_DEBUG=1, for an administrator refused by an SCP or boundary
  await assert.rejects(
    () => db.setConfigEntry("cluster.locale", "en_US.UTF-8"),
    (error: Error) =>
      error.message.includes("IDEA_DEBUG=1") &&
      (error.cause as Error | undefined)?.name === "AccessDeniedException" &&
      String((error.cause as Error).message).includes("dynamodb:UpdateItem"),
  );
  await assert.rejects(() => db.deleteConfigEntries("cluster.upgrade_"), plain);
  // any other failure is left as it was
  context.mock.method(DynamoDBDocumentClient.prototype, "send", async () => {
    throw Object.assign(new Error("throttled"), { name: "ProvisionedThroughputExceededException" });
  });
  await assert.rejects(() => db.setConfigEntry("cluster.locale", "x"), /throttled/);
});
