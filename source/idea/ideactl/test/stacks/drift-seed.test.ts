/**
 * Seed operator edits of every upgrade merge class, run the upgrade settings
 * sequence against DynamoDB Local, and compare survival with the analysis
 * prediction and with the drift preview.
 */

import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import { isDeepStrictEqual } from "node:util";

import { DeleteTableCommand, DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient, ScanCommand, UpdateCommand } from "@aws-sdk/lib-dynamodb";

import type { ConfigWriter, Deps } from "../../src/cli/cdk-invoker.ts";
import { planMetricsProviderCutover, planUpgradePhase3Entries, type UpgradeDeps } from "../../src/cli/commands/upgrade.ts";
import { ClusterConfigDb } from "../../src/config/cluster-config-db.ts";
import {
  compareUpgradeDrift,
  dynamoValueType,
  UPGRADE_DRIFT_ACTIONS,
  type CurrentConfigRow,
  type UpgradeDriftAction,
  type UpgradeDriftEffect,
  type UpgradeDriftFinding,
} from "../../src/config/upgrade-drift.ts";
import type { CfnResponse, JsonValue } from "../../src/lambda/commons/cfn-response.ts";
import {
  createHandler,
  type ClusterSettingsEvent,
} from "../../src/lambda/idea_custom_resource_update_cluster_settings/index.ts";

import {
  BASE_OS,
  BUILT_AMI,
  BUILT_AMI_NAME,
  CLUSTER,
  EDITS,
  GENERATED,
  MODULES,
  REGION,
  RELEASE_AMI,
  SELECTED_STACK,
  STACK_SEED,
  UNSELECTED_STACK,
  type OperatorEdit,
} from "./drift-catalog.ts";
import { startDynamoDbLocal } from "./drift-local-db.ts";

const REPORT = path.resolve(import.meta.dirname, "../../docs/port/analysis/drift-demonstrated.md");
const REPEAT = "node --test 'test/stacks/drift-seed.test.ts'";

const SEQUENCE = [
  REPEAT,
  "java -Djava.library.path=$HOME/.idea/lib/dynamodb-local/DynamoDBLocal_lib -jar $HOME/.idea/lib/dynamodb-local/DynamoDBLocal.jar -inMemory -port <free>",
  `ClusterConfigDb.open({ clusterName: "${CLUSTER}", awsRegion: "${REGION}", createDatabase: true })`,
  "db.syncModulesInDb(MODULES)",
  "db.syncClusterSettingsInDb(GENERATED except ADD keys)",
  "write STACK_SEED with source=stack, then db.setConfigEntry for each seeded operator overlay",
  "db.syncClusterSettingsInDb(generated keys starting with global-settings., overwrite=true)",
  "db.syncClusterSettingsInDb(GENERATED, overwrite=false)",
  "db.setConfigEntry for each Phase 3 entry, then for the metrics provider cutover",
  `planUpgradePhase3Entries(..., amiId=${RELEASE_AMI}, baseOs=${BASE_OS}) then db.setConfigEntry for each planned row`,
  "Custom::ClusterSettings Update for selected module cluster-manager (not metrics)",
].join("\n");

interface Row {
  key: string;
  value: unknown;
  source?: string;
  version?: number;
}

interface EditResult {
  id: string;
  editClass: string;
  key: string;
  predictedSurvive: boolean;
  survived: boolean;
  match: boolean;
  before: unknown;
  after: unknown;
  note: string;
  previewAction: string;
  previewEffect: string;
  actualEffect: string;
}

interface PreviewResult {
  action: UpgradeDriftAction;
  key: string;
  predictedEffect: UpgradeDriftEffect;
  actualEffect: UpgradeDriftEffect;
  match: boolean;
  expectedGap: boolean;
  note: string;
}

function unexpected(name: string): never {
  throw new Error(`unexpected ${name}`);
}

function isJsonValue(value: unknown): value is JsonValue {
  if (value === null) return true;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return true;
  if (Array.isArray(value)) return value.every(isJsonValue);
  if (typeof value === "object") return Object.values(value).every(isJsonValue);
  return false;
}

function jsonSettings(settings: Readonly<Record<string, unknown>>): Record<string, JsonValue> {
  const out: Record<string, JsonValue> = {};
  for (const [key, value] of Object.entries(settings)) {
    if (!isJsonValue(value)) throw new TypeError(`settings.${key} is not a JSON value`);
    out[key] = value;
  }
  return out;
}

/** Scan the settings table through the document client. */
async function loadRows(doc: DynamoDBDocumentClient, table: string): Promise<Map<string, Row>> {
  const rows = new Map<string, Row>();
  let startKey: Record<string, unknown> | undefined;
  do {
    const page = await doc.send(
      new ScanCommand({ TableName: table, ExclusiveStartKey: startKey }),
    );
    for (const item of page.Items ?? []) {
      const key = item["key"];
      if (typeof key !== "string" || key === "") continue;
      const version = item["version"];
      const source = item["source"];
      rows.set(key, {
        key,
        value: item["value"],
        source: typeof source === "string" ? source : undefined,
        version: typeof version === "number" ? version : undefined,
      });
    }
    startKey = page.LastEvaluatedKey;
  } while (startKey !== undefined);
  return rows;
}

function currentRows(rows: Map<string, Row>): CurrentConfigRow[] {
  return [...rows.values()].map((row) => ({
    key: row.key,
    value: row.value,
    source: row.source,
    version: row.version,
  }));
}

function valueEqual(left: unknown, right: unknown): boolean {
  return dynamoValueType(left) === dynamoValueType(right) && isDeepStrictEqual(left, right);
}

function effectOf(before: Row | undefined, after: Row | undefined): UpgradeDriftEffect {
  if (before === undefined && after !== undefined) return "ADD";
  if (before !== undefined && after === undefined) return "DELETE";
  if (before === undefined && after === undefined) return "DELETE";
  if (!valueEqual(before?.value, after?.value)) return "CHANGE";
  return "PRESERVE";
}

/**
 * Preview CHANGE with an unchanged table value is expected for a wholesale
 * rewrite of an equal global row, and for a Phase 3 write of an already-matching
 * operating-system or image key.
 */
function expectedPreviewGap(finding: UpgradeDriftFinding, actualEffect: UpgradeDriftEffect): boolean {
  return (
    (finding.action === "GLOBAL_REWRITE_SAME" || finding.action === "PHASE3_OVERWRITE")
    && finding.effect === "CHANGE"
    && actualEffect === "PRESERVE"
  );
}

function previewMatchesTable(finding: UpgradeDriftFinding, actualEffect: UpgradeDriftEffect): boolean {
  if (finding.action === "GLOBAL_REWRITE_SAME") {
    return expectedPreviewGap(finding, actualEffect);
  }
  return finding.effect === actualEffect || expectedPreviewGap(finding, actualEffect);
}

function phase3Deps(): UpgradeDeps {
  const writer: ConfigWriter = {
    async syncModulesInDb() {
      unexpected("syncModulesInDb");
    },
    async syncClusterSettingsInDb() {
      unexpected("syncClusterSettingsInDb");
    },
    async setConfigEntry() {
      unexpected("setConfigEntry");
    },
    async deleteConfigEntries() {
      unexpected("deleteConfigEntries");
    },
  };
  const base: Deps = {
    async spawn() {
      unexpected("spawn");
    },
    cfn: {
      async describeChangeSet() {
        unexpected("describeChangeSet");
      },
      async executeChangeSet() {
        unexpected("executeChangeSet");
      },
      async describeStack() {
        unexpected("describeStack");
      },
    },
    s3: {
      async putObject() {
        unexpected("putObject");
      },
      async getObject() {
        unexpected("getObject");
      },
    },
    async scan() {
      unexpected("scan");
    },
    async configWriter() {
      return writer;
    },
    async accountId() {
      return "123456789012";
    },
    async httpStatus() {
      return 0;
    },
    async sleep() {},
    now() {
      return 0;
    },
    uuid() {
      return "00000000-0000-0000-0000-000000000000";
    },
    out() {},
    err() {},
    async prompt() {
      return true;
    },
  };
  return {
    ...base,
    ec2: {
      async describeImages() {
        return [
          {
            ImageId: BUILT_AMI,
            Name: BUILT_AMI_NAME,
            CreationDate: "2026-02-02T00:00:00.000Z",
          },
          {
            ImageId: RELEASE_AMI,
            Name: "release",
            CreationDate: "2026-01-01T00:00:00.000Z",
          },
        ];
      },
      async describeInstanceTypeOfferings() {
        return ["m7i.large"];
      },
      async describeInstanceAttribute() {
        unexpected("describeInstanceAttribute");
      },
      async modifyInstanceAttribute() {
        unexpected("modifyInstanceAttribute");
      },
      async createTags() { throw new Error("unexpected tag write"); },
      async deleteTags() { throw new Error("unexpected tag write"); },
      async describeLiveInstances() {
        unexpected("describeLiveInstances");
      },
    },
    cloudFormation: {
      async listStackResources() {
        return { instanceIds: [] };
      },
    },
    openSearch: {
      async describeDomain() {
        return {
          engineVersion: "OpenSearch_2.19",
          serviceSoftwareOptions: { updateAvailable: false, updateStatus: "COMPLETED" },
        };
      },
      async listInstanceTypeDetails() {
        return ["m7g.large.search"];
      },
    },
    eolSoftwareStacks: {
      async setEnabled() {
        unexpected("setEnabled");
      },
      async delete() {
        unexpected("delete");
      },
    },
    async deploy() {
      unexpected("deploy");
    },
  };
}

async function writeStackOwned(
  doc: DynamoDBDocumentClient,
  table: string,
  key: string,
  value: unknown,
): Promise<void> {
  await doc.send(
    new UpdateCommand({
      TableName: table,
      Key: { key },
      UpdateExpression: "SET #value=:value, #source=:source ADD #version :version",
      ExpressionAttributeNames: { "#value": "value", "#version": "version", "#source": "source" },
      ExpressionAttributeValues: { ":value": value, ":source": "stack", ":version": 1 },
    }),
  );
}

async function applySelectedStackUpdate(
  doc: DynamoDBDocumentClient,
  clusterName: string,
): Promise<CfnResponse[]> {
  const responses: CfnResponse[] = [];
  const handler = createHandler({
    dynamoDb: () => ({
      send: async (command) => doc.send(command),
    }),
    logger: { info() {}, error() {} },
    responseSender: async (response) => {
      responses.push(response);
    },
  });
  const event: ClusterSettingsEvent = {
    RequestType: "Update",
    ResponseURL: "https://example.invalid/response",
    StackId: "synthetic-stack",
    RequestId: "synthetic-request",
    LogicalResourceId: "ClusterSettings",
    ResourceProperties: {
      cluster_name: clusterName,
      module_id: SELECTED_STACK.moduleId,
      version: "26.09.0",
      settings: jsonSettings(SELECTED_STACK.target ?? {}),
    },
    OldResourceProperties: {
      cluster_name: clusterName,
      module_id: SELECTED_STACK.moduleId,
      version: "26.08.0",
      settings: jsonSettings(SELECTED_STACK.previous),
    },
  };
  await handler(event, { logStreamName: "drift-seed" });
  return responses;
}

function previewAction(findings: UpgradeDriftFinding[], key: string): string {
  return findings.find((finding) => finding.key === key)?.action ?? "-";
}

function previewEffect(findings: UpgradeDriftFinding[], key: string): string {
  return findings.find((finding) => finding.key === key)?.effect ?? "-";
}

function cell(value: unknown): string {
  if (value === undefined) return "absent";
  return JSON.stringify(value).replaceAll("|", "\\|");
}

function classRollup(edits: EditResult[]): string[] {
  const order: string[] = [];
  const groups = new Map<string, EditResult[]>();
  for (const row of edits) {
    const existing = groups.get(row.editClass);
    if (existing === undefined) {
      order.push(row.editClass);
      groups.set(row.editClass, [row]);
    } else {
      existing.push(row);
    }
  }
  const lines = [
    "| Class | Keys | Predicted | Actual | Match |",
    "|---|---|---|---|---|",
  ];
  for (const editClass of order) {
    const rows = groups.get(editClass) ?? [];
    const predicted = summarizeSurvive(rows.map((row) => row.predictedSurvive));
    const actual = summarizeSurvive(rows.map((row) => row.survived));
    const match = rows.every((row) => row.match) ? "yes" : "NO";
    lines.push(
      `| ${editClass} | ${rows.map((row) => `\`${row.key}\``).join(", ")} | ${predicted} | ${actual} | ${match} |`,
    );
  }
  return lines;
}

function summarizeSurvive(flags: boolean[]): string {
  if (flags.every((flag) => flag)) return "survive";
  if (flags.every((flag) => !flag)) return "lost";
  return "mixed";
}

function renderReport(input: {
  how: string;
  java: string;
  edits: EditResult[];
  preview: PreviewResult[];
  findings: string[];
}): string {
  const losses = input.edits.filter((row) => !row.survived);
  const expectedGaps = input.preview.filter((row) => row.expectedGap);
  const lines = [
    "# Operator-edit survival, demonstrated",
    "",
    "This run used a table the harness controls. It did not read or write a",
    "production cluster. The production-shaped surviving classes are here as",
    "synthetic rows: larger instance types, an integration with a model list, a",
    "debug logging profile, a portal title, a locale with an encoding suffix,",
    "mail sending with a sender, and directory paths. The two loss classes that",
    "production happens not to have are here too: an edit under `global-settings.`",
    "and an edit to a machine image key.",
    "",
    "## Repeat",
    "",
    "From the `ideactl` package root:",
    "",
    "```",
    REPEAT,
    "```",
    "",
    "## Sequence",
    "",
    "Emulator:",
    "",
    "```",
    input.how,
    `java: ${input.java}`,
    "```",
    "",
    "Seeding and upgrade writes, in order:",
    "",
    "```",
    SEQUENCE,
    "```",
    "",
    "Those writes are the same ClusterConfigDb calls as upgrade Phase 2",
    "(sync with overwrite in place, obsolete rows removed after deployment),",
    "Phase 2b (full add-only sync), Phase 3 (`planUpgradePhase3Entries` then",
    "`setConfigEntry`), and Phase 4 (`Custom::ClusterSettings` Update for the",
    "selected module). Phase 4 here updates only cluster-manager. A full upgrade",
    "would apply the same overwrite to every selected stack.",
    "",
    "## Edit class",
    "",
    "Survived means the operator value and DynamoDB type are still in the table.",
    "For ADD, the generated value is what must be present afterwards.",
    "",
    ...classRollup(input.edits),
    "",
    "## Operator edits",
    "",
    "| Class | Key | Predicted | Actual | Match | Before | After | Note |",
    "|---|---|---|---|---|---|---|---|",
  ];
  for (const row of input.edits) {
    lines.push(
      `| ${row.editClass} | \`${row.key}\` | ${row.predictedSurvive ? "survive" : "lost"} | ${
        row.survived ? "survive" : "lost"
      } | ${row.match ? "yes" : "NO"} | ${cell(row.before)} | ${cell(row.after)} | ${row.note} |`,
    );
  }

  lines.push(
    "",
    "## Losses demonstrated",
    "",
  );
  if (losses.length === 0) {
    lines.push("None. That would contradict the analysis.");
  } else {
    for (const row of losses) {
      lines.push(
        `- \`${row.key}\` (${row.editClass}): ${cell(row.before)} -> ${cell(row.after)}. ${row.note}`,
      );
    }
  }

  lines.push(
    "",
    "## Preview versus table",
    "",
    "The preview is `compareUpgradeDrift` with the real Phase 3 plan. Effect is",
    "measured on values, so a global rewrite of an equal value is PRESERVE in the",
    "table and CHANGE in the preview. A Phase 3 write of an already-matching",
    "operating-system or image key is the same gap: the row is written (version",
    "increments) and the value does not change.",
    "",
    "| Action | Key | Preview effect | Table effect | Match | Note |",
    "|---|---|---|---|---|---|",
  );
  for (const row of input.preview) {
    lines.push(
      `| ${row.action} | \`${row.key}\` | ${row.predictedEffect} | ${row.actualEffect} | ${
        row.match ? "yes" : "NO"
      } | ${row.note} |`,
    );
  }

  lines.push("", "## Expected preview gaps", "");
  if (expectedGaps.length === 0) {
    lines.push("None.");
  } else {
    for (const row of expectedGaps) {
      lines.push(
        `- \`${row.key}\` (${row.action}): preview ${row.predictedEffect}, table ${row.actualEffect}. ${row.note}`,
      );
    }
  }

  lines.push("", "## Findings", "");
  if (input.findings.length === 0) {
    lines.push("None. Every analysis prediction matched the table.");
  } else {
    for (const finding of input.findings) lines.push(`- ${finding}`);
  }
  lines.push("");
  return lines.join("\n");
}

function measureEdits(
  before: Map<string, Row>,
  after: Map<string, Row>,
  findings: UpgradeDriftFinding[],
): { rows: EditResult[]; mismatches: string[] } {
  const mismatches: string[] = [];
  const rows = EDITS.map((edit: OperatorEdit) => {
    const beforeRow = before.get(edit.key);
    const afterRow = after.get(edit.key);
    const survived = afterRow !== undefined && valueEqual(afterRow.value, edit.operatorValue);
    const match = survived === edit.predictedSurvive;
    if (!match) {
      mismatches.push(
        `${edit.editClass} ${edit.key}: predicted ${edit.predictedSurvive ? "survive" : "lost"}, actual ${
          survived ? "survive" : "lost"
        }`,
      );
    }
    return {
      id: edit.id,
      editClass: edit.editClass,
      key: edit.key,
      predictedSurvive: edit.predictedSurvive,
      survived,
      match,
      before: beforeRow?.value,
      after: afterRow?.value,
      note: edit.note,
      previewAction: previewAction(findings, edit.key),
      previewEffect: previewEffect(findings, edit.key),
      actualEffect: effectOf(beforeRow, afterRow),
    };
  });
  return { rows, mismatches };
}

test("upgrade settings sync matches the analysis prediction for every edit class", { timeout: 120_000 }, async (t) => {
  const local = await startDynamoDbLocal();
  t.after(() => local.stop());
  t.diagnostic(`DynamoDB Local via ${local.how} at ${local.endpoint}`);

  const { NodeHttpHandler } = await import("@smithy/node-http-handler");
  const client = new DynamoDBClient({
    region: REGION,
    endpoint: local.endpoint,
    credentials: { accessKeyId: "local", secretAccessKey: "local" },
    requestHandler: new NodeHttpHandler({ httpAgent: new http.Agent({ keepAlive: false }) }),
  });
  t.after(() => client.destroy());

  for (const suffix of ["cluster-settings", "modules"]) {
    try {
      await client.send(new DeleteTableCommand({ TableName: `${CLUSTER}.${suffix}` }));
    } catch (error) {
      if (!(error instanceof Error) || error.name !== "ResourceNotFoundException") throw error;
    }
  }

  const db = await ClusterConfigDb.open({
    clusterName: CLUSTER,
    client,
    awsRegion: REGION,
    createDatabase: true,
  });
  const doc = DynamoDBDocumentClient.from(client);

  await db.syncModulesInDb(MODULES.map((module) => ({
    id: module.module_id,
    name: module.name,
    type: module.type,
  })));

  const unseeded = new Set(EDITS.filter((edit) => edit.seed === false).map((edit) => edit.key));
  await db.syncClusterSettingsInDb(GENERATED.filter((entry) => !unseeded.has(entry.key)), false);

  for (const row of STACK_SEED) {
    await writeStackOwned(doc, db.clusterSettingsTableName, row.key, row.value);
  }

  for (const edit of EDITS) {
    if (edit.seed === false) continue;
    await db.setConfigEntry(edit.key, edit.operatorValue);
  }

  const before = await loadRows(doc, db.clusterSettingsTableName);
  const current = currentRows(before);
  const phase3 = await planUpgradePhase3Entries(
    phase3Deps(),
    { awsRegion: REGION },
    MODULES,
    current,
    RELEASE_AMI,
    BASE_OS,
  );

  assert.equal(
    phase3.some((entry) => entry.key === "scheduler.compute_node_ami"),
    false,
    "built compute image must be kept out of Phase 3",
  );
  assert.ok(
    phase3.some((entry) => entry.key === "scheduler.instance_ami" && entry.value === RELEASE_AMI),
    "unrecognised scheduler AMI must be in Phase 3",
  );
  assert.ok(
    phase3.some((entry) => entry.key === "cluster-manager.ec2.autoscaling.instance_type" && entry.value === "m7i.large"),
    "retained m6i.large must be in Phase 3",
  );
  assert.ok(
    phase3.some((entry) => entry.key === "analytics.opensearch.data_node_instance_type" && entry.value === "m7g.large.search"),
    "retained OpenSearch default must be in Phase 3",
  );

  const providerCutover = planMetricsProviderCutover(GENERATED, current);
  assert.ok(
    providerCutover.some((entry) => entry.key === "metrics.provider" && entry.value === "dogstatsd"),
    "the values file's provider must be in the cutover",
  );

  const drift = compareUpgradeDrift({
    current,
    generated: GENERATED,
    phase3,
    providerCutover,
    stacks: [SELECTED_STACK, UNSELECTED_STACK],
    replaceGlobalSettings: true,
    syncFullConfiguration: true,
  });

  const previewActions = new Set(drift.findings.map((finding) => finding.action));
  for (const action of UPGRADE_DRIFT_ACTIONS) {
    assert.ok(previewActions.has(action), `preview missing action ${action}`);
  }

  await db.syncClusterSettingsInDb(
    GENERATED.filter((entry) => entry.key.startsWith("global-settings.")),
    true,
  );
  await db.syncClusterSettingsInDb(GENERATED, false);
  for (const entry of [...phase3, ...providerCutover]) {
    await db.setConfigEntry(entry.key, entry.value);
  }
  const stackResponses = await applySelectedStackUpdate(doc, CLUSTER);
  assert.equal(stackResponses.length, 1);
  assert.equal(stackResponses[0]?.status, "SUCCESS");
  // After every stack has deployed the rows this release no longer generates are removed, one
  // exact key at a time.
  const generatedGlobal = new Set(GENERATED.filter((entry) => entry.key.startsWith("global-settings.")).map((entry) => entry.key));
  for (const key of [...before.keys()].filter((key) => key.startsWith("global-settings.") && !generatedGlobal.has(key))) {
    await db.deleteConfigEntries(key);
  }

  const after = await loadRows(doc, db.clusterSettingsTableName);
  const measured = measureEdits(before, after, drift.findings);
  const findings = [...measured.mismatches];

  const previewRows = drift.findings.map((finding) => {
    const actualEffect = effectOf(before.get(finding.key), after.get(finding.key));
    const expectedGap = expectedPreviewGap(finding, actualEffect);
    const match = previewMatchesTable(finding, actualEffect);
    const note = expectedGap
      ? finding.action === "GLOBAL_REWRITE_SAME"
        ? "Preview CHANGE is the delete-and-recreate. The operator value is unchanged."
        : "Phase 3 writes the row again. The value already matched, so the table value is unchanged."
      : "";
    if (!match) {
      findings.push(
        `preview ${finding.action} ${finding.key}: effect ${finding.effect}, table ${actualEffect}`,
      );
    }
    return {
      action: finding.action,
      key: finding.key,
      predictedEffect: finding.effect,
      actualEffect,
      match,
      expectedGap,
      note,
    };
  });

  mkdirSync(path.dirname(REPORT), { recursive: true });
  writeFileSync(
    REPORT,
    renderReport({
      how: local.how,
      java: local.java,
      edits: measured.rows,
      preview: previewRows,
      findings,
    }),
  );

  t.diagnostic(`wrote ${REPORT}`);
  t.diagnostic(SEQUENCE);
  for (const row of measured.rows) {
    t.diagnostic(
      `${row.match ? "ok" : "MISMATCH"} ${row.editClass} ${row.key} predicted=${
        row.predictedSurvive ? "survive" : "lost"
      } actual=${row.survived ? "survive" : "lost"}`,
    );
  }

  const rewriteSame = previewRows.filter((row) => row.action === "GLOBAL_REWRITE_SAME");
  assert.ok(rewriteSame.length > 0, "preview must classify a same-value global rewrite");
  for (const row of rewriteSame) {
    assert.equal(row.predictedEffect, "CHANGE", `${row.key} preview effect`);
    assert.equal(row.actualEffect, "PRESERVE", `${row.key} table effect`);
    assert.equal(row.expectedGap, true, `${row.key} is the documented preview gap`);
  }

  assert.equal(findings.length, 0, findings.join("; "));
  for (const row of measured.rows) {
    assert.equal(row.match, true, `${row.editClass} ${row.key}`);
  }

  const added = after.get("cluster.brand_new");
  assert.equal(added?.value, true);
  assert.equal(after.get("cluster.timezone")?.value, "America/New_York");

  // Rewritten in place now, so the row keeps its identity and its version only grows.
  const rewritten = after.get("global-settings.same");
  assert.ok((rewritten?.version ?? 0) >= 1);
  assert.equal(rewritten?.source, "template");

  const locale = after.get("cluster.locale");
  assert.equal(locale?.value, "en_US.UTF-8");
  assert.equal(locale?.version, 2);

  const rewrittenOs = after.get("cluster-manager.ec2.autoscaling.base_os");
  assert.equal(rewrittenOs?.value, BASE_OS);
  assert.equal(rewrittenOs?.version, 2);

  assert.deepEqual(after.get("global-settings.custom_tags")?.value, []);
  assert.equal(after.has("global-settings.operator_only"), false);
  assert.equal(after.get("scheduler.instance_ami")?.value, RELEASE_AMI);
  assert.equal(after.get("scheduler.compute_node_ami")?.value, BUILT_AMI);
  assert.equal(after.get("cluster-manager.ec2.autoscaling.instance_type")?.value, "m7i.large");
  assert.equal(after.get("analytics.opensearch.data_node_instance_type")?.value, "m7g.large.search");
  assert.equal(after.get("cluster-manager.external_alb_dns")?.value, "stack.example.invalid");
  assert.equal(after.has("cluster-manager.removed_output"), false);
  assert.equal(after.get("metrics.external_alb_dns")?.value, "metrics-operator.example.invalid");
});
